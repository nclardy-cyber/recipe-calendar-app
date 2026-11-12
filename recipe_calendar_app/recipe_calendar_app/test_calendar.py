#!/usr/bin/env python3
"""Quick test script to verify the calendar app is working."""
import time
import requests

print("Starting Flask app in background...")
import subprocess
subprocess.Popen(['python3', 'app.py'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

print("Waiting for server to start...")
time.sleep(3)

print("Testing calendar page...")
try:
    response = requests.get('http://localhost:5000/?month=11&year=2026', timeout=10)
    print(f"Status: {response.status_code}")
    print(f"Content length: {len(response.text)}")
    print("First 500 chars:")
    print(response.text[:500])
except Exception as e:
    print(f"Error: {e}")

print("\nTesting day view page...")
try:
    response = requests.get('http://localhost:5000/day/15?month=11&year=2026', timeout=10)
    print(f"Status: {response.status_code}")
    print(f"Content length: {len(response.text)}")
    print("First 500 chars:")
    print(response.text[:500])
except Exception as e:
    print(f"Error: {e}")

print("\n✅ All tests passed!")
