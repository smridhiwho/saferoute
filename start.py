#!/usr/bin/env python3
"""
SafeRoute startup script.
Checks environment, installs deps, starts server.
"""
import os, sys, subprocess

def check_env():
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if not os.path.exists(env_path):
        print("\n  No .env file found. Creating from template...")
        import shutil
        shutil.copy(env_path + '.example', env_path)
        print("  Created .env - please add your API keys:")
        print("    ORS_API_KEY=your_key_here")
        print("    GROQ_API_KEY=your_key_here")
        print("\n  Then run: python start.py\n")
        sys.exit(1)

    # Check keys
    ors_ok = False
    groq_ok = False
    with open(env_path) as f:
        for line in f:
            if 'ORS_API_KEY=' in line and 'your_ors_key_here' not in line:
                ors_ok = True
            if 'GROQ_API_KEY=' in line and 'your_groq_key_here' not in line:
                groq_ok = True

    print(f"  ORS API key:  {'✓ set' if ors_ok else '✗ missing (routing will fail)'}")
    print(f"  Groq API key: {'✓ set' if groq_ok else '✗ missing (AI advice disabled, app still works)'}")
    if not ors_ok:
        print("\n  Get a free ORS key at: https://openrouteservice.org/dev/#/signup")
        print("  Get a free Groq key at: https://console.groq.com\n")

def install_deps():
    req = os.path.join(os.path.dirname(__file__), 'requirements.txt')
    print("\n  Installing dependencies...")
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', req, '-q'], check=True)

if __name__ == '__main__':
    print("\n  SafeRoute MVP")
    print("  =============")
    check_env()
    install_deps()
    print("\n  Starting server...\n")
    os.chdir(os.path.dirname(__file__))
    os.execv(sys.executable, [sys.executable, 'app.py'])
