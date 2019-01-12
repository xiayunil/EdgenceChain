from typing import (
    Iterable, NamedTuple, Dict, Mapping, Union, get_type_hints, Tuple,
    Callable)
from utils.Errors import (BaseException, TxUnlockError, TxnValidationError, BlockValidationError)

from utils.Utils import Utils
from params.Params import Params
from wallet.Wallet import Wallet
from dataStructure.UnspentTxOut import UnspentTxOut


import binascii,ecdsa,logging



logging.basicConfig(
    level=getattr(logging, os.environ.get('TC_LOG_LEVEL', 'INFO')),
    format='[%(asctime)s][%(module)s:%(lineno)d] %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# Used to represent the specific output within a transaction.
OutPoint = NamedTuple('OutPoint', [('txid', str), ('txout_idx', int)])


class TxIn(NamedTuple):
    """Inputs to a Transaction."""
    # A reference to the output we're spending. This is None for coinbase
    # transactions.
    to_spend: Union[OutPoint, None]

    # The (signature, pubkey) pair which unlocks the TxOut for spending.
    unlock_sig: bytes
    unlock_pk: bytes

    # A sender-defined sequence number which allows us replacement of the txn
    # if desired.
    sequence: int


class TxOut(NamedTuple):
    """Outputs from a Transaction."""
    # The number of LET this awards.
    value: int

    # The public key of the owner of this Txn.
    to_address: str


class Transaction(NamedTuple):
    txins: Iterable[TxIn]
    txouts: Iterable[TxOut]

    # The block number or timestamp at which this transaction is unlocked.
    # < 500000000: Block number at which this transaction is unlocked.
    # >= 500000000: UNIX timestamp at which this transaction is unlocked.
    locktime: int = None

    @property
    def is_coinbase(self) -> bool:
        return len(self.txins) == 1 and self.txins[0].to_spend is None

    @classmethod
    def create_coinbase(cls, pay_to_addr, value, height):
        return cls(
            txins=[TxIn(
                to_spend=None,
                # Push current block height into unlock_sig so that this
                # transaction's ID is unique relative to other coinbase txns.
                unlock_sig=str(height).encode(),
                unlock_pk=None,
                sequence=0)],
            txouts=[TxOut(
                value=value,
                to_address=pay_to_addr)],
        )

    @property
    def id(self) -> str:
        return Utils.sha256d(Utils.serialize(self))

    def validate_basics(self, as_coinbase=False):
        if (not self.txouts) or (not self.txins and not as_coinbase):
            raise TxnValidationError('Missing txouts or txins')

        if len(Utils.serialize(self)) > Params.MAX_BLOCK_SERIALIZED_SIZE:
            raise TxnValidationError('Too large')

        if sum(t.value for t in self.txouts) > Params.MAX_MONEY:
            raise TxnValidationError('Spend value too high')


    def validate_txn(self,
                     as_coinbase: bool = False,
                     siblings_in_block: Iterable[object] = None,  #object
                     allow_utxo_from_mempool: bool = True,
                     ) -> bool:
        """
        Validate a single transaction. Used in various contexts, so the
        parameters facilitate different uses.
        """

        def validate_signature_for_spend(txin, utxo: UnspentTxOut, txn):
            def build_spend_message(to_spend, pk, sequence, txouts) -> bytes:
                """This should be ~roughly~ equivalent to SIGHASH_ALL."""
                return Utils.sha256d(
                    Utils.serialize(to_spend) + str(sequence) +
                    binascii.hexlify(pk).decode() + Utils.serialize(txouts)).encode()

            pubkey_as_addr = Wallet.pubkey_to_address(txin.unlock_pk)
            verifying_key = ecdsa.VerifyingKey.from_string(
                txin.unlock_pk, curve=ecdsa.SECP256k1)

            if pubkey_as_addr != utxo.to_address:
                raise TxUnlockError("Pubkey doesn't match")

            try:
                spend_msg = build_spend_message(
                    txin.to_spend, txin.unlock_pk, txin.sequence, txn.txouts)
                verifying_key.verify(txin.unlock_sig, spend_msg)
            except Exception:
                logger.exception('Key verification failed')
                raise TxUnlockError("Signature doesn't match")
            return True        


        self.validate_basics(as_coinbase=as_coinbase)

        available_to_spend = 0

        for i, txin in enumerate(self.txins):
            utxo = utxo_set.get(txin.to_spend)

            if siblings_in_block:
                utxo = utxo or find_utxo_in_list(txin, siblings_in_block)

            if allow_utxo_from_mempool:
                utxo = utxo or find_utxo_in_mempool(txin)

            if not utxo:
                raise TxnValidationError(
                    f'Could find no UTXO for TxIn[{i}] -- orphaning txn',
                    to_orphan=self)

            if utxo.is_coinbase and \
                    (get_current_height() - utxo.height) < \
                    Params.COINBASE_MATURITY:
                raise TxnValidationError(f'Coinbase UTXO not ready for spend')

            try:
                validate_signature_for_spend(txin, utxo)
            except TxUnlockError:
                raise TxnValidationError(f'{txin} is not a valid spend of {utxo}')

            available_to_spend += utxo.value

        if available_to_spend < sum(o.value for o in cls.txouts):
            raise TxnValidationError('Spend value is more than available')

        return True



