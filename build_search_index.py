"""Compatibility entry point for the resumable Phase 2 index builder."""
from search_index import build

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='Build/resume the Phase 2 identifier index.')
    ap.add_argument('--rebuild', action='store_true', help='Delete the index and start over (normally NOT needed).')
    ap.add_argument('--batch', type=int, default=10000)
    args = ap.parse_args()
    build(rebuild=args.rebuild, batch=args.batch)
