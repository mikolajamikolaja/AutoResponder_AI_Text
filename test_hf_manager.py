#!/usr/bin/env python3
"""
Test script for hf_token_manager migration.
"""

import sys
import os

sys.path.insert(0, ".")

print("Testing hf_token_manager...")

try:
    from core.hf_token_manager import get_active_tokens, mark_dead, is_dead

    print("Import OK")
except Exception as e:
    print(f"Import error: {e}")
    sys.exit(1)

# Get active tokens
tokens = get_active_tokens()
print(f"Number of active tokens: {len(tokens)}")
for name, val in tokens[:3]:
    print(f"  {name}: {val[:10]}...")

# Test is_dead with a fake token
fake = "HF_TOKEN_FAKE"
print(f"\nIs {fake} dead? {is_dead(fake)}")

# Test mark_dead (just a call, no actual token)
try:
    mark_dead(fake, reason="test")
    print(f"Marked {fake} as dead")
except Exception as e:
    print(f"mark_dead error: {e}")

# Check again
print(f"Is {fake} dead now? {is_dead(fake)}")

# Test that get_active_tokens still works after marking dead (should not include fake)
tokens2 = get_active_tokens()
print(f"\nActive tokens after marking fake dead: {len(tokens2)}")

print("\nTest completed successfully.")
