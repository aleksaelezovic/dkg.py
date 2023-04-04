from dkg.module import Module
from dkg.method import Method
from dkg.dataclasses import NodeResponseDict
from dkg.types import UAL, Address, JSONLD, HexStr, NQuads, AgreementData
from dkg.utils.node_request import NodeRequest, validate_operation_status, StoreTypes
from dkg.utils.blockchain_request import BlockchainRequest
from dkg.manager import DefaultRequestManager
from dkg.utils.ual import parse_ual, format_ual
from dkg.utils.decorators import retry
from dkg.utils.rdf import normalize_dataset
from dkg.exceptions import OperationNotFinished, InvalidAsset
from web3 import Web3
from web3.exceptions import ContractLogicError
from pyld import jsonld
from dkg.utils.merkle import MerkleTree, hash_assertion_with_indexes
from dkg.utils.metadata import generate_assertion_metadata, generate_keyword, generate_agreement_id
from typing import Literal
import math
from typing import Type


class ContentAsset(Module):
    HASH_FUNCTION_ID = 1
    SCORE_FUNCTION_ID = 1
    PRIVATE_ASSERTION_PREDICATE = 'https://ontology.origintrail.io/dkg/1.0#privateAssertionID'

    def __init__(self, manager: DefaultRequestManager):
        self.manager = manager

    _chain_id = Method(BlockchainRequest.chain_id)

    _get_contract_address = Method(BlockchainRequest.get_contract_address)
    _get_asset_storage_address = Method(BlockchainRequest.get_asset_storage_address)
    _increase_allowance = Method(BlockchainRequest.increase_allowance)
    _decrease_allowance = Method(BlockchainRequest.decrease_allowance)
    _create = Method(BlockchainRequest.create_asset)

    _get_bid_suggestion = Method(NodeRequest.bid_suggestion)
    _local_store = Method(NodeRequest.local_store)
    _publish = Method(NodeRequest.publish)

    def create(
        self,
        content: dict[Literal['public', 'private'], JSONLD],
        epochs_number: int,
        token_amount: int | None = None,
        immutable: bool = False,
        content_type: Literal['JSON-LD', 'N-Quads'] = 'JSON-LD',
    ) -> dict[str, HexStr | dict[str, str]]:
        assertions = self._process_content(content, content_type)

        chain_name = self.manager.blockchain_provider.SUPPORTED_NETWORKS[self._chain_id()]
        content_asset_storage_address = self._get_asset_storage_address('ContentAssetStorage')

        if token_amount is None:
            token_amount = int(
                self._get_bid_suggestion(
                    chain_name,
                    epochs_number,
                    assertions["public"]["size"],
                    content_asset_storage_address,
                    assertions["public"]["id"],
                    self.HASH_FUNCTION_ID,
                )['bidSuggestion']
            )

        service_agreement_v1_address = str(self._get_contract_address('ServiceAgreementV1'))
        self._increase_allowance(service_agreement_v1_address, token_amount)

        try:
            receipt = self._create({
                'assertionId': Web3.to_bytes(hexstr=assertions["public"]["id"]),
                'size': assertions["public"]["size"],
                'triplesNumber': assertions["public"]["triples_number"],
                'chunksNumber': assertions["public"]["chunks_number"],
                'tokenAmount': token_amount,
                'epochsNumber': epochs_number,
                'scoreFunctionId': self.SCORE_FUNCTION_ID,
                'immutable_': immutable,
            })
        except ContractLogicError as err:
            self._decrease_allowance(service_agreement_v1_address, token_amount)
            raise err

        events = self.manager.blockchain_provider.decode_logs_event(
            receipt,
            'ContentAsset',
            'AssetMinted',
        )
        token_id = events[0].args['tokenId']

        assertions_list = [{
            'blockchain': chain_name,
            'contract': content_asset_storage_address,
            'tokenId': token_id,
            'assertionId': assertions["public"]["id"],
            'assertion': assertions["public"]["content"],
            'storeType': StoreTypes.TRIPLE.value,
        }]

        if content.get('private', None):
            assertions_list.append({
                'blockchain': chain_name,
                'contract': content_asset_storage_address,
                'tokenId': token_id,
                'assertionId': assertions["private"]["id"],
                'assertion': assertions["private"]["content"],
                'storeType': StoreTypes.TRIPLE.value,
            })

        operation_id = self._local_store(assertions_list)['operationId']
        self.get_operation_result(operation_id, 'local-store')

        operation_id = self._publish(
            assertions["public"]["id"],
            assertions["public"]["content"],
            chain_name,
            content_asset_storage_address,
            token_id,
            self.HASH_FUNCTION_ID,
        )['operationId']
        operation_result = self.get_operation_result(operation_id, 'publish')

        return {
            'UAL': format_ual(chain_name, content_asset_storage_address, token_id),
            'publicAssertionId': assertions["public"]["id"],
            'operation': {
                'operationId': operation_id,
                'status': operation_result['status'],
            },
        }

    _update = Method(NodeRequest.update)

    _get_block = Method(BlockchainRequest.get_block)

    _get_service_agreement_data = Method(BlockchainRequest.get_service_agreement_data)
    _update_asset_state = Method(BlockchainRequest.update_asset_state)

    def update(
        self,
        ual: UAL,
        content: dict[Literal['public', 'private'], JSONLD],
        token_amount: int | None = None,
        content_type: Literal['JSON-LD', 'N-Quads'] = 'JSON-LD',
    ) -> dict[str, HexStr | dict[str, str]]:
        parsed_ual = parse_ual(ual)
        content_asset_storage_address, token_id = parsed_ual['contract_address'], parsed_ual['token_id']

        assertions = self._process_content(content, content_type)

        chain_name = self.manager.blockchain_provider.SUPPORTED_NETWORKS[self._chain_id()]

        if token_amount is None:
            agreement_id = self.get_agreement_id(content_asset_storage_address, token_id)
            # TODO: Dynamic types for namedtuples?
            agreement_data: Type[AgreementData] = self._get_service_agreement_data(agreement_id)

            timestamp_now = self._get_block("latest")["timestamp"]
            current_epoch = math.floor(
                (timestamp_now - agreement_data.startTime) / agreement_data.epochLength
            )
            epochs_left = agreement_data.epochsNumber - current_epoch

            token_amount = int(
                self._get_bid_suggestion(
                    chain_name,
                    epochs_left,
                    assertions["public"]["size"],
                    content_asset_storage_address,
                    assertions["public"]["id"],
                    self.HASH_FUNCTION_ID,
                )['bidSuggestion']
            )

            token_amount -= agreement_data.tokensInfo[0]
            token_amount = token_amount if token_amount > 0 else 0

        self._update_asset_state(
            token_id=token_id,
            assertion_id=assertions["public"]["id"],
            size=assertions["public"]["size"],
            triples_number=assertions["public"]["triples_number"],
            chunks_number=assertions["public"]["chunks_number"],
            update_token_amount=token_amount,
        )

        assertions_list = [{
            'blockchain': chain_name,
            'contract': content_asset_storage_address,
            'tokenId': token_id,
            'assertionId': assertions["public"]["id"],
            'assertion': assertions["public"]["content"],
            'storeType': StoreTypes.PENDING.value,
        }]

        if content.get('private', None):
            assertions_list.append({
                'blockchain': chain_name,
                'contract': content_asset_storage_address,
                'tokenId': token_id,
                'assertionId': assertions["private"]["id"],
                'assertion': assertions["private"]["content"],
                'storeType': StoreTypes.PENDING.value,
            })

        operation_id = self._local_store(assertions_list)['operationId']
        self.get_operation_result(operation_id, 'local-store')

        operation_id = self._update(
            assertions["public"]["id"],
            assertions["public"]["content"],
            chain_name,
            content_asset_storage_address,
            token_id,
            self.HASH_FUNCTION_ID,
        )['operationId']
        operation_result = self.get_operation_result(operation_id, 'update')

        return {
            'UAL': format_ual(chain_name, content_asset_storage_address, token_id),
            'publicAssertionId': assertions["public"]["id"],
            'operation': {
                'operationId': operation_id,
                'status': operation_result['status'],
            },
        }

    _get_latest_assertion_id = Method(BlockchainRequest.get_latest_assertion_id)

    _get = Method(NodeRequest.get)

    def get(
        self, ual: UAL, validate: bool = False
    ) -> dict[str, HexStr | list[JSONLD] | dict[str, str]]:
        operation_id: NodeResponseDict = self._get(ual, hashFunctionId=1)['operationId']

        @retry(catch=OperationNotFinished, max_retries=5, base_delay=1, backoff=2)
        def get_operation_result() -> NodeResponseDict:
            operation_result = self._get_operation_result(
                operation='get',
                operation_id=operation_id,
            )

            validate_operation_status(operation_result)

        operation_result = get_operation_result()
        assertion = operation_result['data']['assertion']

        token_id = parse_ual(ual)['token_id']
        latest_assertion_id = Web3.to_hex(self._get_latest_assertion_id(token_id))

        if validate:
            root = MerkleTree(hash_assertion_with_indexes(assertion), sort_pairs=True).root
            if root != latest_assertion_id:
                raise InvalidAsset(
                    f'Latest assertionId: {latest_assertion_id}. '
                    f'Merkle Tree Root: {root}'
                )

        assertion_json_ld: list[JSONLD] = jsonld.from_rdf(
            '\n'.join(assertion),
            {'algorithm': 'URDNA2015', 'format': 'application/n-quads'}
        )

        return {
            'assertionId': latest_assertion_id,
            'assertion': assertion_json_ld,
            'operation': {
                'operation_id': operation_id,
                'status': operation_result['status'],
            },
        }

    _owner = Method(BlockchainRequest.owner_of)

    def owner(self, token_id: int) -> Address:
        return self._owner(token_id)

    def _process_content(
        self,
        content: dict[Literal['public', 'private'], JSONLD],
        type: Literal['JSON-LD', 'N-Quads'] = 'JSON-LD',
    ) -> dict[str, dict[str, HexStr | NQuads | int]]:
        public_graph = {'@graph': []}

        if content.get('public', None):
            public_graph['@graph'].append(content['public'])

        if content.get('private', None):
            private_assertion = normalize_dataset(content['private'], type)
            private_assertion_id = MerkleTree(
                hash_assertion_with_indexes(private_assertion),
                sort_pairs=True,
            ).root

            public_graph['@graph'].append(
                {self.PRIVATE_ASSERTION_PREDICATE: private_assertion_id}
            )

        public_assertion = normalize_dataset(public_graph, type)
        public_assertion_id = MerkleTree(
            hash_assertion_with_indexes(public_assertion),
            sort_pairs=True,
        ).root
        public_assertion_metadata = generate_assertion_metadata(public_assertion)

        return {
            "public": {
                "id": public_assertion_id,
                "content": public_assertion,
                **public_assertion_metadata,
            },
            "private": {
                "id": private_assertion_id,
                "content": private_assertion,
            } if content.get("private", None) else {},
        }

    _get_assertion_id_by_index = Method(BlockchainRequest.get_assertion_id_by_index)

    def get_agreement_id(self, contract_address: Address, token_id: int) -> HexStr:
        first_assertion_id = self._get_assertion_id_by_index(token_id, 0)
        keyword = generate_keyword(contract_address, first_assertion_id)
        return generate_agreement_id(contract_address, token_id, keyword)

    _get_operation_result = Method(NodeRequest.get_operation_result)

    @retry(catch=OperationNotFinished, max_retries=5, base_delay=1, backoff=2)
    def get_operation_result(self, operation_id: str, operation: str) -> NodeResponseDict:
        operation_result = self._get_operation_result(
            operation_id=operation_id,
            operation=operation,
        )

        validate_operation_status(operation_result)

        return operation_result


class Assets(Module):
    content: ContentAsset
