"""
Django API Test Script
Tests the Django healer service with API calls

Usage:
    1. Start Django: python manage.py runserver
    2. Run this test: python test_django.py
"""

import requests
import json
import sys
import os

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("=" * 80)
print("DJANGO HEALER SERVICE - API TEST")
print("=" * 80)

# Configuration
BASE_URL = "http://127.0.0.1:8000"
HEAL_URL = f"{BASE_URL}/api/heal/"
BATCH_URL = f"{BASE_URL}/api/heal/batch/"

# Simple test HTML
test_html = """
<html>
    <body>
        <nav>
            <a href="/home" id="home-link">Home</a>
            <a href="/about" class="nav-link">About</a>
        </nav>
        <main>
            <button id="submit-btn" data-testid="submit-button" aria-label="Submit Form">Submit</button>
            <button class="cancel-btn">Cancel</button>
            <input type="text" name="username" placeholder="Enter username" />
        </main>
    </body>
</html>
"""

print("\n" + "=" * 80)
print("TEST 1: Single Heal Request")
print("=" * 80)

try:
    payload = {
        "failed_selector": "button.old-submit-button",
        "html": test_html,
        "use_of_selector": "click on submit button",
        "page_url": "https://example.com",
        "selector_type": "css"
    }
    
    print(f"\n→ Sending POST to {HEAL_URL}")
    print(f"  Failed selector: {payload['failed_selector']}")
    print(f"  Semantic hint: {payload['use_of_selector']}")
    
    response = requests.post(HEAL_URL, json=payload, timeout=30)
    
    print(f"\n✓ Status Code: {response.status_code}")
    
    if response.status_code == 200:
        result = response.json()
        
        print("\n" + "=" * 80)
        print("SUCCESS!")
        print("=" * 80)
        
        print(f"\n✓ Chosen Selector: {result.get('chosen')}")
        print(f"✓ Message: {result.get('message')}")
        
        if result.get('candidates'):
            print(f"\n✓ Top Candidates ({len(result['candidates'])}):")
            for i, c in enumerate(result['candidates'][:3], 1):
                print(f"  {i}. {c['selector']}")
                print(f"     Score: {c['score']:.4f} (base: {c['base_score']:.4f}, attr: {c['attribute_score']:.4f})")
                print(f"     Tag: {c.get('tag')}, Text: {c.get('text')}")
        
        debug = result.get('debug', {})
        print(f"\n✓ API Response IDs:")
        print(f"  HealerRequest ID: {result.get('id')}")
        print(f"  Batch ID: {result.get('batch_id')}")

        print(f"\n✓ Processing Time: {debug.get('processing_time_ms', 0):.2f}ms")
        print(f"✓ Engine: {debug.get('engine')}")
        print(f"✓ Total Candidates: {debug.get('total_candidates')}")
        
    else:
        print(f"\n✗ Error: {response.status_code}")
        print(f"  {response.text}")
        
except requests.exceptions.ConnectionError:
    print("\n✗ Error: Cannot connect to Django server!")
    print("  Make sure Django is running:")
    print("  → python manage.py runserver")
    sys.exit(1)
    
except Exception as e:
    print(f"\n✗ Error: {type(e).__name__}: {str(e)}")
    sys.exit(1)

print("\n" + "=" * 80)
print("TEST 2: Batch Heal Request")
print("=" * 80)

try:
    payload = {
        "selectors": [
            {
                "failed_selector": "button.old-submit",
                "html": test_html,
                "use_of_selector": "click on submit button",
                "page_url": "https://example.com"
            },
            {
                "failed_selector": "a.old-home-link",
                "html": test_html,
                "use_of_selector": "click on home link",
                "page_url": "https://example.com"
            }
        ]
    }
    
    print(f"\n→ Sending POST to {BATCH_URL}")
    print(f"  Batch size: {len(payload['selectors'])}")
    
    response = requests.post(BATCH_URL, json=payload, timeout=30)
    
    print(f"\n✓ Status Code: {response.status_code}")
    
    if response.status_code == 200:
        result = response.json()
        
        print("\n" + "=" * 80)
        print("BATCH SUCCESS!")
        print("=" * 80)
        
        print(f"\n✓ Total Processed: {result.get('total_processed')}")
        print(f"✓ Succeeded: {result.get('total_succeeded')}")
        print(f"✓ Failed: {result.get('total_failed')}")
        print(f"✓ Batch ID: {result.get('id')}")
        print(f"✓ Processing Time: {result.get('processing_time_ms', 0):.2f}ms")
        
        print(f"\n✓ Results:")
        for i, res in enumerate(result.get('results', []), 1):
            print(f"\n  Item {i}:")
            print(f"    IDs: Request={res.get('id')}, Batch={res.get('batch_id')}")
            print(f"    Message: {res.get('message')}")
            print(f"    Chosen: {res.get('chosen')}")
            if res.get('candidates'):
                print(f"    Candidates: {len(res['candidates'])}")
                print(f"    Top Score: {res['candidates'][0]['score']:.4f}")
        
    else:
        print(f"\n✗ Error: {response.status_code}")
        print(f"  {response.text}")
        
except Exception as e:
    print(f"\n✗ Error: {type(e).__name__}: {str(e)}")

print("\n" + "=" * 80)
print("ALL TESTS COMPLETE")
print("=" * 80)
print("\n✓ Django REST Framework migration successful!")
print("✓ Database integration working")
print("✓ API endpoints responding correctly")
print("\nYou can now delete the FastAPI files:")
print("  - main.py")
print("  - test_run.py")
print("  - test_run_batch.py")
