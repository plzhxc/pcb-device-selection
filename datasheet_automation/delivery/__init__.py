"""Delivery domain: turn one MinerU parse into the handoff the downstream consumes.

- manifest: prune debug dumps, validate assets, write the self-describing manifest.json
- flatten:  flatten a nested parse into the flat delivery dir with canonical image names
- card:     render the LCSC product card (<code>_card.md), market section included
"""
