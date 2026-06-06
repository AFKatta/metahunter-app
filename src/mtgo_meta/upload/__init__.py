"""Auto-uploader: ship local matches up to the central server.

Layered as:

    state.py    install identity (install_id + install_salt)
    consent.py  user's consent record + leaderboard preference
    client.py   thin HTTP wrapper around metahunter-api.fly.dev
    worker.py   background thread that pushes new matches in batches
"""
