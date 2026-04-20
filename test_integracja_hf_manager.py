#!/usr/bin/env python3
"""
Test integracji hf_token_manager z responderami.
Uruchamia importy i sprawdza, czy funkcje get_active_tokens działają.
"""
import sys
import os
sys.path.insert(0, '.')

print("=== Test integracji hf_token_manager ===")

# 1. Test core.hf_token_manager
print("\n1. Test core.hf_token_manager...")
try:
    from core.hf_token_manager import get_active_tokens, mark_dead, hf_tokens
    tokens = get_active_tokens()
    print(f"   get_active_tokens() zwrócił {len(tokens)} tokenów")
    print(f"   hf_tokens.all_dead() = {hf_tokens.all_dead()}")
    print("   ✓ core.hf_token_manager OK")
except Exception as e:
    print(f"   ✗ core.hf_token_manager błąd: {e}")
    sys.exit(1)

# 2. Test responders.zwykly (import i funkcje)
print("\n2. Test responders.zwykly...")
try:
    import responders.zwykly
    # Sprawdź, czy używa get_active_tokens
    from responders.zwykly import _png_to_jpg
    print("   ✓ import OK, funkcje dostępne")
except Exception as e:
    print(f"   ✗ responders.zwykly błąd: {e}")

# 3. Test responders.smierc (import)
print("\n3. Test responders.smierc...")
try:
    import responders.smierc
    # Sprawdź, czy używa get_active_tokens
    from responders.smierc import _generate_flux_image
    print("   ✓ import OK, funkcje dostępne")
except Exception as e:
    print(f"   ✗ responders.smierc błąd: {e}")

# 4. Test responders.zwykly_psychiatryczny_raport (import)
print("\n4. Test responders.zwykly_psychiatryczny_raport...")
try:
    import responders.zwykly_psychiatryczny_raport
    from responders.zwykly_psychiatryczny_raport import build_raport, _parse_json_safe
    print("   ✓ import OK, funkcje dostępne")
except Exception as e:
    print(f"   ✗ responders.zwykly_psychiatryczny_raport błąd: {e}")

# 5. Sprawdź, czy w config nie ma HF_TOKEN_BLACKLIST
print("\n5. Sprawdzenie core/config.py...")
try:
    from core.config import *
    if 'HF_TOKEN_BLACKLIST' in dir():
        print("   ✗ HF_TOKEN_BLACKLIST nadal istnieje w config!")
    else:
        print("   ✓ HF_TOKEN_BLACKLIST nie ma w config")
except Exception as e:
    print(f"   ✗ błąd import config: {e}")

# 6. Symulacja użycia get_active_tokens w kontekście aplikacji
print("\n6. Symulacja użycia get_active_tokens w kontekście aplikacji...")
try:
    # Ustawienie zmiennych środowiskowych testowych
    os.environ['HF_TOKEN'] = 'fake_token_for_test'
    os.environ['HF_TOKEN1'] = 'fake_token1'
    
    tokens = get_active_tokens()
    print(f"   Po dodaniu zmiennych środowiskowych: {len(tokens)} tokenów")
    if len(tokens) >= 1:
        name, token = tokens[0]
        print(f"   Pierwszy token: {name}")
        # Test mark_dead
        mark_dead(name, reason="test integration")
        print(f"   Oznaczono {name} jako dead")
        print(f"   is_dead({name}) = {name in hf_tokens._dead}")
    else:
        print("   Brak tokenów - sprawdź zmienne środowiskowe")
except Exception as e:
    print(f"   ✗ symulacja błąd: {e}")

print("\n=== Test zakończony ===")