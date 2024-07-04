import json
import time
import logging
import sys
import typing
import asyncio
import random
from classes import LogsSubscriptionHandler
from solana.rpc.async_api import AsyncClient
from colorama import Fore, init
from typing import Dict, List
from solders.signature import Signature

init(autoreset=True)

Stats = Dict[str, object]
rpc_stats: Dict[str, Stats] = {}  # {url: {stats}}
current_subscriptions: typing.Dict[int, LogsSubscriptionHandler] = {}
rpc_urls: List[str] = []
test_time: int = 60
sub_count: int = 0
sampling_fraction: float = 0.00  # fraction of signatures to sample
output_file: str = "rpc_stats.json"

# CLI Arguments
n = len(sys.argv)
if n < 5:
    print("Usage: python main.py <input_file> <test_time> <sampling_fraction> <output_file>")
    sys.exit(1)

# read in CLI arguments
input_file = sys.argv[1]
test_time = int(sys.argv[2])
sampling_fraction = float(sys.argv[3])
output_file = sys.argv[4]

# Read RPC URLs from file
try:
    with open(input_file, "r") as f:
        rpc_urls = f.read().splitlines()
except FileNotFoundError:
    logging.error(f"File {sys.argv[1]} not found")
    sys.exit(1)

if not rpc_urls:
    logging.error("No RPC URLs found in file. Exiting.")
    sys.exit(1)

# Pre-test message
pre_test_message = f"""{Fore.YELLOW}Test Summary{Fore.RESET}:
-----------------------------------------------------------
This script will run a benchmark test for {test_time} seconds on each RPC URL.
-----------------------------------------------------------
It will subscribe to {len(rpc_urls)} RPC URLs.
-----------------------------------------------------------
Estimated test time: {round((test_time * len(rpc_urls) / 60), 2)} minutes.
-----------------------------------------------------------
The test will begin in 5 seconds."""
print(pre_test_message)
time.sleep(5)

# Unsubscribe after test_time seconds
async def unsubscribe_after_timeout(subscription_key: int, duration: int):
    await asyncio.sleep(duration)
    if subscription_key in current_subscriptions:
        rpc_url = current_subscriptions[subscription_key].url
        await current_subscriptions.pop(subscription_key).unsubscribe()
        end_time = time.time()
        if rpc_url in rpc_stats:
            rpc_stats[rpc_url]["end_time"] = end_time

async def simple_callback(ctx: AsyncClient, data: str):
    response_time = time.time()
    rpc_url = ctx._provider.endpoint_uri  # Get the URL from the client context
    json_data = json.loads(data)

    if rpc_url not in rpc_stats:
        rpc_stats[rpc_url] = {
            "first_response_time": response_time,
            "total_responses": 0,
            "total_time": 0,
            "total_latency": 0,
            "average_latency": 0,
            "signatures": [],
            "sampled_latencies": [],
            "response_times": {}  # New field to store response times for each signature
        }
    else:
        if "first_response_time" not in rpc_stats[rpc_url]:
            rpc_stats[rpc_url]["first_response_time"] = response_time

    stats = rpc_stats[rpc_url]
    stats["total_responses"] += 1
    stats["total_time"] = response_time - stats["first_response_time"]

    if json_data["result"]["value"]["err"] is None:
        sig_string = json_data["result"]["value"]["signature"]
        stats["signatures"].append(sig_string)
        stats["response_times"][sig_string] = response_time  # Store the response time for each signature

async def fetch_transaction_latency(ctx: AsyncClient, signature: str, received_time: float):
    try:
        transaction: solders.rpc.responses.GetTransactionResp = await ctx.get_transaction(Signature.from_string(signature), max_supported_transaction_version=0, commitment="confirmed", encoding="jsonParsed")
        # sleep to avoid rate limiting
        await asyncio.sleep(0.07)
        if transaction != "1111111111111111111111111111111111111111111111111111111111111111":
            block_time = transaction.value.block_time
            if block_time:
                latency = received_time - block_time  # Use the stored response time
                return latency
            else:
                logging.error(f"Block time not found for transaction: {signature}")
        else:
            logging.error(f"Transaction not found: {signature}")

    except Exception as e:
        logging.error(f"Error fetching transaction: {e} for signature: {signature}")
    return None

async def run_logs_subscription(url: str, test_time: int):
    global sub_count
    subscription = LogsSubscriptionHandler({
        "rpc": url,
        "http": url.replace("wss", "https").replace("ws", "http"),
    })
    start_time = time.time()
    current_subscriptions[sub_count] = subscription
    asyncio.create_task(unsubscribe_after_timeout(sub_count, test_time))
    sub_count += 1
    await subscription.listen(simple_callback)  # using await to only run one subscription at a time - likely not a good idea to do concurrent subs due to throughput limitations
    end_time = time.time()
    if url in rpc_stats:
        rpc_stats[url]["total_test_time"] = end_time - start_time

async def main():
    tasks = [await run_logs_subscription(url, test_time) for url in rpc_urls]

    # After collecting signatures, fetch transactions for a random sample
    for rpc_url, stats in rpc_stats.items():
        if stats["signatures"]:
            sample_size = max(1, int(len(stats["signatures"]) * sampling_fraction))
            sampled_signatures = random.sample(stats["signatures"], sample_size)
            latencies = await asyncio.gather(*[fetch_transaction_latency(AsyncClient(rpc_url), sig, stats["response_times"][sig]) for sig in sampled_signatures])
            latencies = [latency for latency in latencies if latency is not None]

            if latencies:
                stats["sampled_latencies"] = latencies
                stats["total_latency"] = sum(latencies)
                stats["average_latency"] = stats["total_latency"] / len(latencies)
                stats["responses_per_second"] = stats["total_responses"] / stats["total_time"]

    # write to file
    with open(output_file, "w") as f:
        json.dump(rpc_stats, f, indent=4)

if __name__ == "__main__":
    asyncio.run(main())
