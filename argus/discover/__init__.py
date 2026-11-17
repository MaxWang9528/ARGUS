"""Stream discovery: find the top live Twitch streams with the official Helix API.

- ``ranking``: pure functions (Helix results + settings -> ranked list; keep_minutes hold).
- ``helix``: a small Helix client (app access token, ``/streams`` paging, ``/games``).
- ``service``: ``StreamDiscovery``, the component the app runs.
"""
