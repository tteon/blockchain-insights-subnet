import asyncio
import json
import threading
import time
import uuid
from datetime import datetime
from random import sample
from typing import cast, Dict

from communex.client import CommuneClient  # type: ignore
from communex.misc import get_map_modules
from communex.module.client import ModuleClient  # type: ignore
from communex.module.module import Module  # type: ignore
from communex.types import Ss58Address  # type: ignore
from substrateinterface import Keypair  # type: ignore
from ._config import ValidatorSettings
from loguru import logger

from .database.models import validation_prompt_response
from .database.models.challenge_balance_tracking import ChallengeBalanceTrackingManager
from .database.models.challenge_funds_flow import ChallengeFundsFlowManager
from .database.models.validation_prompt_response import ValidationPromptResponseManager
from .encryption import generate_hash
from .helpers import raise_exception_if_not_registered, get_ip_port, cut_to_max_allowed_weights
from .llm.base_llm import BaseLLM
from .llm.factory import LLMFactory
from .nodes.factory import NodeFactory
from .weights_storage import WeightsStorage
from src.subnet.validator.database.models.miner_discovery import MinerDiscoveryManager
from src.subnet.validator.database.models.miner_receipts import MinerReceiptManager, ReceiptMinerRank
from src.subnet.protocol.llm_engine import LlmQueryRequest, LlmMessage, Challenge, LlmMessageList, ChallengesResponse, \
    ChallengeMinerResponse, LlmMessageOutputList, MODEL_TYPE_FUNDS_FLOW
from src.subnet.protocol.blockchain import Discovery
from src.subnet.validator.database.models.validation_prompt import ValidationPromptManager


class Validator(Module):

    def __init__(
            self,
            key: Keypair,
            netuid: int,
            client: CommuneClient,
            weights_storage: WeightsStorage,
            miner_discovery_manager: MinerDiscoveryManager,
            validation_prompt_manager: ValidationPromptManager,
            validation_prompt_response_manager: ValidationPromptResponseManager,
            challenge_funds_flow_manager: ChallengeFundsFlowManager,
            challenge_balance_tracking_manager: ChallengeBalanceTrackingManager,
            miner_receipt_manager: MinerReceiptManager,
            llm: BaseLLM,
            query_timeout: int = 60,
            llm_query_timeout: int = 60,
            challenge_timeout: int = 60,

    ) -> None:
        super().__init__()

        self.miner_receipt_manager = miner_receipt_manager
        self.client = client
        self.key = key
        self.netuid = netuid
        self.llm = llm
        self.llm_query_timeout = llm_query_timeout
        self.challenge_timeout = challenge_timeout
        self.query_timeout = query_timeout
        self.weights_storage = weights_storage
        self.miner_discovery_manager = miner_discovery_manager
        self.terminate_event = threading.Event()
        self.validation_prompt_manager = validation_prompt_manager
        self.validation_prompt_response_manager = validation_prompt_response_manager
        self.challenge_funds_flow_manager = challenge_funds_flow_manager
        self.challenge_balance_tracking_manager = challenge_balance_tracking_manager

    @staticmethod
    def get_addresses(client: CommuneClient, netuid: int) -> dict[int, str]:
        modules_adresses = client.query_map_address(netuid)
        for id, addr in modules_adresses.items():
            if addr.startswith('None'):
                port = addr.split(':')[1]
                modules_adresses[id] = f'0.0.0.0:{port}'
        return modules_adresses

    async def _challenge_miner(self, miner_info):
        start_time = time.time()
        try:
            connection, miner_metadata = miner_info
            module_ip, module_port = connection
            miner_key = miner_metadata['key']
            client = ModuleClient(module_ip, int(module_port), self.key)

            logger.info(f"Challenging miner {miner_key}")

            # Discovery Phase
            discovery = await self._get_discovery(client, miner_key)
            if not discovery:
                return None

            logger.debug(f"Got discovery for miner {miner_key}")

            # Challenge Phase
            node = NodeFactory.create_node(discovery.network)
            challenge_response = await self._perform_challenges(client, miner_key, discovery, node)
            if not challenge_response:
                return None

            # Prompt Phase
            random_validation_prompt, prompt_model_type, prompt_result_expected = await self.validation_prompt_manager.get_random_prompt(discovery.network)
            if not random_validation_prompt:
                logger.error("Failed to get a random validation prompt")
                return None

            llm_message_list = LlmMessageList(messages=[LlmMessage(type=0, content=random_validation_prompt)])
            prompt_result_actual = await self._send_prompt(client, miner_key, llm_message_list)
            if not prompt_result_actual:
                return None

            logger.info(f"Prompt result actual is {prompt_result_actual}")

            validation_result = await Validator.validate_query_by_prompt(
                random_validation_prompt=random_validation_prompt,
                miner_key=miner_key,
                miner_query=prompt_result_actual.outputs[0].result,
                network=discovery.network,
                validation_prompt_response_manager=self.validation_prompt_response_manager,
                llm=self.llm
            )

            return ChallengeMinerResponse(
                network=discovery.network,
                funds_flow_challenge_actual=challenge_response.funds_flow_challenge_actual,
                funds_flow_challenge_expected=challenge_response.funds_flow_challenge_expected,
                balance_tracking_challenge_actual=challenge_response.balance_tracking_challenge_actual,
                balance_tracking_challenge_expected=challenge_response.balance_tracking_challenge_expected,
                query_validation_result = validation_result
            )
        except Exception as e:
            logger.error(f"Failed to challenge miner {miner_key}, {e}")
            return None
        finally:
            end_time = time.time()
            execution_time = end_time - start_time
            logger.info(f"Execution time for challenge_miner {miner_key}: {execution_time} seconds")

    async def _get_discovery(self, client, miner_key) -> Discovery:
        try:
            discovery = await client.call(
                "discovery",
                miner_key,
                {},
                timeout=self.challenge_timeout,
            )

            return Discovery(**discovery)
        except Exception as e:
            logger.info(f"Miner {miner_key} failed to get discovery")
            return None

    async def _perform_challenges(self, client, miner_key, discovery, node) -> ChallengesResponse | None:
        try:
            # Funds flow challenge
            funds_flow_challenge, tx_id = await self.challenge_funds_flow_manager.get_random_challenge(discovery.network)
            funds_flow_challenge = Challenge.model_validate_json(funds_flow_challenge)
            funds_flow_challenge = await client.call(
                "challenge",
                miner_key,
                {"challenge": funds_flow_challenge.model_dump()},
                timeout=self.challenge_timeout,
            )
            funds_flow_challenge = Challenge(**funds_flow_challenge)
            logger.debug(f"Funds flow challenge result for {miner_key}: {funds_flow_challenge.output}")

            # Balance tracking challenge
            balance_tracking_challenge, balance_tracking_expected_response = await self.challenge_balance_tracking_manager.get_random_challenge(discovery.network)
            balance_tracking_challenge = Challenge.model_validate_json(balance_tracking_challenge)
            balance_tracking_challenge = await client.call(
                "challenge",
                miner_key,
                {"challenge": balance_tracking_challenge.model_dump()},
                timeout=self.challenge_timeout,
            )
            balance_tracking_challenge = Challenge(**balance_tracking_challenge)
            logger.debug(f"Balance tracking challenge result for {miner_key}: {balance_tracking_challenge.output}")

            return ChallengesResponse(
                funds_flow_challenge_actual=funds_flow_challenge.output['tx_id'],
                funds_flow_challenge_expected=tx_id,
                balance_tracking_challenge_actual=balance_tracking_challenge.output['balance'],
                balance_tracking_challenge_expected=balance_tracking_expected_response,
            )
        except Exception as e:
            logger.error(f"Miner {miner_key} failed to perform challenges: {e}")
            return None

    async def _send_prompt(self, client, miner_key, llm_message_list) -> LlmMessageOutputList | None:
        try:
            llm_query_result = await client.call(
                "llm_query_v2",
                miner_key,
                {"llm_messages_list": llm_message_list.model_dump()},
                timeout=self.llm_query_timeout,
            )
            if not llm_query_result:
                return None

            return LlmMessageOutputList(**llm_query_result)
        except Exception as e:
            logger.info(f"Miner {miner_key} failed to generate an answer")
            return None

    @staticmethod
    def _score_miner(response: ChallengeMinerResponse, receipt_miner_multiplier: float) -> float:
        if not response:
            logger.info(f"Miner didn't answer")
            return 0

        failed_challenges = response.get_failed_challenges()
        if failed_challenges > 0:
            if failed_challenges == 2:
                return 0
            else:
                return 0.15

        score = 0.3

        if response.query_validation_result is None:
            return score

        similarity_score = 0
        if response.query_validation_result == 'valid':
            logger.info("Scoring: Valid")
            similarity_score = 1
        #similarity_score = fuzzy_json_similarity(
        #    response.prompt_result_actual,
        #    response.prompt_result_expected,
        #    numeric_tolerance=0.05, string_threshold=80)
        similarity_score = 0
        score = score + (0.3 * similarity_score)

        multiplier = min(1, receipt_miner_multiplier)
        score = score + (0.4 * multiplier)

        return score

    @staticmethod
    async def validate_query_by_prompt(random_validation_prompt: str, miner_key: str, miner_query: str, network: str,
                                       validation_prompt_response_manager: ValidationPromptResponseManager, llm) -> str:
        # Retrieve cached query from DB using the miner_key and random_validation_prompt
        cached_query = await validation_prompt_response_manager.get_response_by_prompt_and_miner(
            random_validation_prompt, miner_key)

        # If a cached query is found, compare it to the miner's query
        if cached_query:
            logger.info(f"Cached query found: {cached_query}")

            # Compare the cached query with the actual query from the miner
            if cached_query == miner_query:
                logger.info("Miner's query matches the cached query")
                return "valid"
            else:
                logger.info("Miner's query does not match the cached query")
                return "invalid"

        # If no cached query is found, store the miner's query in the DB
        logger.info("No cached query found, storing the new query in the cache")
        await validation_prompt_response_manager.store_response(
            random_validation_prompt,
            miner_key,
            miner_query
        )

        # Since no cached query was found, use the LLM to validate the query
        logger.info("Using LLM for validation")
        validation_result = llm.validate_query_by_prompt(random_validation_prompt, miner_query, network)

        return validation_result

    async def validate_step(self, netuid: int, settings: ValidatorSettings
                            ) -> None:

        score_dict: dict[int, float] = {}
        miners_module_info = {}

        modules = cast(dict[str, Dict], get_map_modules(self.client, netuid=netuid, include_balances=False))
        modules_addresses = self.get_addresses(self.client, netuid)
        ip_ports = get_ip_port(modules_addresses)

        raise_exception_if_not_registered(self.key, modules)

        for key in modules.keys():
            module_meta_data = modules[key]
            uid = module_meta_data['uid']
            stake = module_meta_data['stake']
            if stake > 5000:
                logger.debug(f"Skipping module {uid} with stake {stake} as it probably is not a miner")
                continue
            if uid not in ip_ports:
                logger.debug(f"Skipping module {uid} as it doesn't have an IP address")
                continue
            module_addr = ip_ports[uid]
            miners_module_info[uid] = (module_addr, modules[key])

        logger.info(f"Found the following miners: {miners_module_info.keys()}")

        logger.debug("Updating miner ranks")
        for _, miner_metadata in miners_module_info.values(): # this is intentionally in this place
            await self.miner_discovery_manager.update_miner_rank(miner_metadata['key'], miner_metadata['emission'])

        challenge_tasks = []
        for uid, miner_info in miners_module_info.items():
            challenge_tasks.append(self._challenge_miner(miner_info))

        logger.debug(f"Challenging {len(challenge_tasks)} miners")
        responses: tuple[ChallengeMinerResponse] = await asyncio.gather(*challenge_tasks)
        logger.debug(f"Got responses from {len(responses)} miners")

        for uid, miner_info, response in zip(miners_module_info.keys(), miners_module_info.values(), responses):
            if not response:
                score_dict[uid] = 0
                continue

            if isinstance(response, ChallengeMinerResponse):
                network = response.network
                connection, miner_metadata = miner_info
                miner_address, miner_ip_port = connection
                miner_key = miner_metadata['key']
                receipt_miner_multiplier = await self.miner_receipt_manager.get_receipt_miner_multiplier(miner_key)
                score = self._score_miner(response, receipt_miner_multiplier)
                assert score <= 1
                score_dict[uid] = score

                await self.miner_discovery_manager.store_miner_metadata(uid, miner_key, miner_address, miner_ip_port, network)
                await self.miner_discovery_manager.update_miner_challenges(miner_key, response.get_failed_challenges(), 2)

        if not score_dict:
            logger.info("No miner managed to give a valid answer")
            return None

        try:
            self.set_weights(settings, score_dict, self.netuid, self.client, self.key)
            logger.info("Weights set")
        except Exception as e:
            logger.error(f"Failed to set weights: {e}")

    def set_weights(self,
                    settings: ValidatorSettings,
                    score_dict: dict[
                        int, float
                    ],
                    netuid: int,
                    client: CommuneClient,
                    key: Keypair,
                    ) -> None:

        score_dict = cut_to_max_allowed_weights(score_dict, settings.MAX_ALLOWED_WEIGHTS)
        self.weights_storage.setup()
        weighted_scores: dict[int, int] = self.weights_storage.read()

        logger.debug(f"Setting weights: {score_dict}")
        score_sum = sum(score_dict.values())

        if score_sum == 0:
            logger.warning("No scores to distribute")
            return

        for uid, score in score_dict.items():
            weight = int(score * 1000 / score_sum)
            weighted_scores[uid] = weight

        # filter out 0 weights
        weighted_scores = {k: v for k, v in weighted_scores.items() if v != 0}
        weighted_scores = {k: v for k, v in weighted_scores.items() if k in score_dict}

        self.weights_storage.store(weighted_scores)

        uids = list(weighted_scores.keys())
        weights = list(weighted_scores.values())

        # send the blockchain call
        logger.debug(f"Sending weights to the blockchain: {weighted_scores}")
        client.vote(key=key, uids=uids, weights=weights, netuid=netuid)

    async def validation_loop(self, settings: ValidatorSettings) -> None:
        while not self.terminate_event.is_set():
            start_time = time.time()
            await self.validate_step(self.netuid, settings)
            if self.terminate_event.is_set():
                logger.info("Terminating validation loop")
                break

            elapsed = time.time() - start_time
            if elapsed < settings.ITERATION_INTERVAL:
                sleep_time = settings.ITERATION_INTERVAL - elapsed
                logger.info(f"Sleeping for {sleep_time}")
                self.terminate_event.wait(sleep_time)
                if self.terminate_event.is_set():
                    logger.info("Terminating validation loop")
                    break

    """ VALIDATOR API METHODS"""
    async def query_miner(self, request: LlmQueryRequest) -> dict:
        request_id = str(uuid.uuid4())
        timestamp = datetime.utcnow()
        prompt_dict = [message.model_dump() for message in request.prompt]
        prompt_hash = generate_hash(json.dumps(prompt_dict))
        llm_message_list = LlmMessageList(messages=request.prompt)

        if request.miner_key:
            miner = await self.miner_discovery_manager.get_miner_by_key(request.miner_key, request.network)
            if not miner:
                return {
                    "request_id": request_id,
                    "timestamp": timestamp,
                    "miner_keys": [],
                    "prompt_hash": prompt_hash,
                    "response": []}

            result = await self._query_miner(miner, llm_message_list)

            await self.miner_receipt_manager.store_miner_receipt(request_id, request.miner_key, prompt_hash, timestamp)

            return {
                "request_id": request_id,
                "timestamp": timestamp,
                "miner_keys": [request.miner_key],
                "prompt_hash": prompt_hash,
                "response": result,
            }
        else:
            select_count = 3
            sample_size = 16
            miners = await self.miner_discovery_manager.get_miners_by_network(request.network)

            if len(miners) < 3:
                top_miners = miners
            else:
                top_miners = sample(miners[:sample_size], select_count)

            query_tasks = []
            for miner in top_miners:
                query_tasks.append(self._query_miner(miner, llm_message_list))

            responses = await asyncio.gather(*query_tasks)

            for miner, response in zip(top_miners, responses):
                if response:
                    await self.miner_receipt_manager.store_miner_receipt(request_id, miner['miner_key'], prompt_hash, timestamp)

            return {
                "request_id": request_id,
                "timestamp": timestamp,
                "miner_keys": [miner['miner_key'] for miner in top_miners],
                "prompt_hash": prompt_hash,
                "response": responses,
            }

    async def _query_miner(self, miner, llm_message_list: LlmMessageList):
        miner_key = miner['miner_key']
        miner_network = miner['network']
        module_ip = miner['miner_address']
        module_port = int(miner['miner_ip_port'])
        module_client = ModuleClient(module_ip, module_port, self.key)
        try:
            llm_query_result = await module_client.call(
                "llm_query",
                miner_key,
                {"llm_messages_list": llm_message_list.model_dump()},
                timeout=self.llm_query_timeout,
            )
            if not llm_query_result:
                return None

            return LlmMessageOutputList(**llm_query_result)
        except Exception as e:
            logger.warning(f"Failed to query miner {miner_key}, {e}")
            return None
