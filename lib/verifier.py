# Electrum - Lightweight Bitcoin Client
# Copyright (c) 2012 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
from .util import ThreadJob, bh2u
from .bitcoin import Hash, hash_decode, hash_encode
from . import networks
from .transaction import Transaction

class SPV(ThreadJob):
    """ Simple Payment Verification """

    def __init__(self, network, wallet):
        self.wallet = wallet
        self.network = network
        self.blockchain = network.blockchain()
        self.merkle_roots = {}  # txid -> merkle root (once it has been verified)
        self.requested_merkle = set()  # txid set of pending requests
        self.qbusy = False
        self.cleaned_up = False
        self._need_release = False

    def _release(self):
        ''' Called from the Network (DaemonThread) -- to prevent race conditions
        with network, we remove data structures related to the network and
        unregister ourselves as a job from within the Network thread itself. '''
        self._need_release = False
        self.cleaned_up = True
        self.network.cancel_requests(self.verify_merkle)
        self.network.remove_jobs([self])

    def release(self):
        ''' Called from main thread, enqueues a 'release' to happen in the
        Network thread. '''
        self._need_release = True

    def run(self):
        if self._need_release:
            self._release()
        if self.cleaned_up:
            return
        interface = self.network.interface
        if not interface:
            self.spam_error("v.no interface")
            return

        blockchain = interface.blockchain
        if not blockchain:
            self.spam_error("v.no blockchain", interface.server)
            return

        local_height = self.network.get_local_height()
        unverified = self.wallet.get_unverified_txs()
        for tx_hash, tx_height in unverified.items():
            # do not request merkle branch if we already requested it
            if tx_hash in self.requested_merkle or tx_hash in self.merkle_roots:
                continue
            # or before headers are available
            if tx_height <= 0 or tx_height > local_height:
                continue

            # if it's in the checkpoint region, we still might not have the header
            header = blockchain.read_header(tx_height)
            if header is None:
                if tx_height <= networks.net.VERIFICATION_BLOCK_HEIGHT:
                    # Per-header requests might be a lot heavier.
                    # Also, they're not supported as header requests are
                    # currently designed for catching up post-checkpoint headers.
                    index = tx_height // 2016
                    if self.network.request_chunk(interface, index):
                        interface.print_error("verifier requesting chunk {} for height {}".format(index, tx_height))
                continue
            # enqueue request
            msg_id = self.network.get_merkle_for_transaction(tx_hash, tx_height,
                                                             self.verify_merkle)
            self.qbusy = msg_id is None
            if self.qbusy:
                # interface queue busy, will try again later
                break
            self.print_error('requested merkle', tx_hash)
            self.requested_merkle.add(tx_hash)

        if self.network.blockchain() != self.blockchain:
            self.blockchain = self.network.blockchain()
            self.undo_verifications()

    def verify_merkle(self, response):
        if self.cleaned_up:
            return  # we have been killed, this was just an orphan callback
        if response.get('error'):
            # FIXME: tx will never verify now until server reconnect.
            self.print_error('received an error:', response)
            return
        params = response['params']
        merkle = response['result']
        # Verify the hash of the server-provided merkle branch to a
        # transaction matches the merkle root of its block
        tx_hash = params[0]
        tx_height = merkle.get('block_height')
        pos = merkle.get('pos')
        try:
            merkle_root = self.hash_merkle_root(merkle['merkle'], tx_hash, pos)
        except Exception as e:
            self.print_error(f"exception while verifying tx {tx_hash}: {repr(e)}")
            return

        header = self.network.blockchain().read_header(tx_height)
        # FIXME: if verification fails below,
        # we should make a fresh connection to a server to
        # recover from this, as this TX will now never verify
        if not header:
            self.print_error(
                "merkle verification failed for {} (missing header {})"
                .format(tx_hash, tx_height))
            return
        if header.get('merkle_root') != merkle_root:
            self.print_error(
                "merkle verification failed for {} (merkle root mismatch {} != {})"
                .format(tx_hash, header.get('merkle_root'), merkle_root))
            return
        # we passed all the tests
        self.merkle_roots[tx_hash] = merkle_root
        # note: we could pop in the beginning, but then we would request
        # this proof again in case of verification failure from the same server
        self.requested_merkle.discard(tx_hash)
        self.print_error("verified %s" % tx_hash)
        self.wallet.add_verified_tx(tx_hash, (tx_height, header.get('timestamp'), pos))
        if self.is_up_to_date() and self.wallet.is_up_to_date() and not self.qbusy:
            self.wallet.save_verified_tx(write=True)
            self.network.trigger_callback('wallet_updated', self.wallet)  # This callback will happen very rarely.. mostly right as the last tx is verified. It's to ensure GUI is updated fully.

    @classmethod
    def hash_merkle_root(cls, merkle_s, target_hash, pos):
        h = hash_decode(target_hash)
        for i, item in enumerate(merkle_s):
            h = Hash(hash_decode(item) + h) if ((pos >> i) & 1) else Hash(h + hash_decode(item))
            # An attack was once upon a time possible for SPV, before Nov. 2018
            # which is described here:
            #
            # https://lists.linuxfoundation.org/pipermail/bitcoin-dev/2018-June/016105.html
            # https://lists.linuxfoundation.org/pipermail/bitcoin-dev/attachments/20180609/9f4f5b1f/attachment-0001.pdf
            # https://bitcoin.stackexchange.com/questions/76121/how-is-the-leaf-node-weakness-in-merkle-trees-exploitable/76122#76122
            #
            # As such, at this point we used to verify the inner node didn't
            # "look" like a tx using some heuristics (which had a very small
            # chance of returning false positives, about 1 in quadrillion).
            #
            # We no longer need do the "inner node looks like tx" check here,
            # however, since no such attack has occurred on the BTC or BCH chain
            # before Nov. 2018. After Nov. 2018 the tx size is now required to
            # be >= 100 bytes which is larger than the 64 byte size of inner
            # nodes.  Thus, the check is rendered superfluous as the attack
            # itself is now no longer even possible after Nov. 2018's hard fork,
            # and so was removed.
            #
            # TL;DR: There used to be some strange check here. It's gone now.
            # Check git history if you're really curious. :)
        return hash_encode(h)

    def undo_verifications(self):
        height = self.blockchain.get_base_height()
        tx_hashes = self.wallet.undo_verifications(self.blockchain, height)
        for tx_hash in tx_hashes:
            self.print_error("redoing", tx_hash)
            self.remove_spv_proof_for_tx(tx_hash)
        self.qbusy = False

    def remove_spv_proof_for_tx(self, tx_hash):
        self.merkle_roots.pop(tx_hash, None)
        self.requested_merkle.discard(tx_hash)

    def is_up_to_date(self):
        return not self.requested_merkle
