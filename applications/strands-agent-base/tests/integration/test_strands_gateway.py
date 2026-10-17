#!/usr/bin/env python3
"""Integration test: Strands Agent through Bifrost AI Gateway."""

import logging
import os
import sys
from strands import Agent
from strands.models.openai import OpenAIModel

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Override via env vars for local testing:
#   BIFROST_URL=http://localhost:8080/v1
#   BIFROST_VK=<your platform VK>
BIFROST_URL = os.getenv("BIFROST_URL", "http://bifrost.bifrost.svc.cluster.local:8080/v1")
BIFROST_VK  = os.getenv("BIFROST_VK", os.getenv("LLM_GATEWAY_API_KEY", ""))
MODEL_ID    = os.getenv("MODEL_ID", "claude-sonnet")


def test_strands_agent():
    print(f"Bifrost URL : {BIFROST_URL}")
    print(f"Model       : {MODEL_ID}")
    print(f"VK present  : {'yes' if BIFROST_VK else 'NO — set BIFROST_VK env var'}")
    print()

    model = OpenAIModel(
        client_args={
            "api_key": BIFROST_VK or "not-used",
            "base_url": BIFROST_URL,
            "default_headers": {"x-bf-vk": BIFROST_VK},
        },
        model_id=MODEL_ID,
        params={"max_tokens": 256},
    )

    agent = Agent(model=model, system_prompt="You are a helpful assistant.")
    response = agent("Hello! Please respond with a short greeting.")
    print(f"Response: {response}")
    return True


if __name__ == "__main__":
    try:
        test_strands_agent()
        print("\n✅ Test passed!")
        sys.exit(0)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\n❌ Test failed: {e}")
        sys.exit(1)
