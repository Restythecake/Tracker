import os

# Unit-Tests fragen nie echte Shops, Nominatim oder Marktpreise ab.
os.environ.setdefault("TRACKER_OFFLINE", "1")
