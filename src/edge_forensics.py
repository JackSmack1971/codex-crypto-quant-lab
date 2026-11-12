#!/usr/bin/env python3
"""Reproducible Base EDGE ERC-20 transfer audit, October 12-November 11 UTC.

Uses public Blockscout API v2; requires outbound HTTPS access. Does NOT
infer purchase, sale, CEX identity, or common wallet ownership from ERC-20
transfers. Produces per-address raw CSV and exact transfer aggregates.
"""
import argparse
import csv
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

TOKEN = '0xED6E000dEF95780fb89734c07EE2ce9F6dcAf110'
START = '2026-10-12T00:00:00'
END = '2026-11-11T00:00:00'
ADDRESSES = {
    'published_buyback_vault': '0xB115124f277215BF85b710F0137EfaCAC09CbAAF',
    'published_staking_contract': '0xdefed6eE363Cc984756566726073ed4d0BC68bde',
    'unattributed_holder_1': '0x421af0f2af4d61e6f8fd356dd66baef66dbe36da',
    'unattributed_holder_2': '0xf6bade4ef7a1887e09fa5d86fdff4b5fd1aef071',
    'unattributed_holder_3': '0x3c716d252b53891753de883894ff29a9ebee0f54',
    'unattributed_holder_4': '0x3db843ddbda69e53f0da67263cd479246fb2fe8f',
}
FIELDS = ['scope', 'timestamp', 'tx_hash', 'log_index', 'from', 'to', 'amount_edge',
          'from_label', 'to_label', 'block_number']


def fetch_json(url):
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'EDGE-forensic-research/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=35) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                raise
            if attempt == 4:
                raise
            time.sleep(min(2 ** attempt, 16))


def scan(scope, address, max_pages):
    base = f'https://base.blockscout.com/api/v2/addresses/{address}/token-transfers'
    cursor = None
    rows, seen = [], set()
    complete = False
    pages = 0
    while pages < max_pages:
        params = {'type': 'ERC-20', 'token': TOKEN}
        if cursor:
            params.update(cursor)
        payload = fetch_json(base + '?' + urllib.parse.urlencode(params))
        items = payload.get('items', [])
        pages += 1
        for item in items:
            timestamp = item.get('timestamp', '')[:19]
            if not (START <= timestamp < END):
                continue
            if item.get('token', {}).get('address_hash', '').lower() != TOKEN.lower():
                continue
            key = (item.get('transaction_hash'), item.get('log_index'))
            if key in seen:
                continue
            seen.add(key)
            amount = Decimal(item['total']['value']) / (Decimal(10) ** int(item['total']['decimals']))
            rows.append({
                'scope': scope, 'timestamp': timestamp,
                'tx_hash': item.get('transaction_hash'), 'log_index': item.get('log_index'),
                'from': item.get('from', {}).get('hash'), 'to': item.get('to', {}).get('hash'),
                'amount_edge': str(amount), 'from_label': item.get('from', {}).get('name'),
                'to_label': item.get('to', {}).get('name'),
                'block_number': item.get('block_number'),
            })
        cursor = payload.get('next_page_params')
        oldest = items[-1].get('timestamp', '')[:19] if items else ''
        if not cursor or (oldest and oldest < START):
            complete = True
            break
        if pages % 25 == 0:
            print(f'{scope}: {pages} pages, {len(rows)} matching events, oldest={oldest}', flush=True)
        time.sleep(0.05)
    # Do not claim coverage if stopped by a page limit.
    return rows, {'complete': complete, 'pages': pages, 'events': len(rows),
                  'oldest_seen': oldest, 'next_page_params': None if complete else cursor}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='edge_forensics_output')
    parser.add_argument('--max-pages', type=int, default=10000)
    parser.add_argument('--only', choices=['all', *ADDRESSES], default='all')
    args = parser.parse_args()
    dest = Path(args.output)
    dest.mkdir(parents=True, exist_ok=True)
    scans = ADDRESSES if args.only == 'all' else {args.only: ADDRESSES[args.only]}
    summary = {'token': TOKEN, 'start_utc': START + 'Z', 'end_exclusive_utc': END + 'Z',
               'warning': 'Transfers do not establish purchases, sales, beneficiaries or CEX deposits.', 'scopes': {}}
    allrows = []
    for label, address in scans.items():
        rows, coverage = scan(label, address, args.max_pages)
        inflow = sum((Decimal(r['amount_edge']) for r in rows if r['to'].lower() == address.lower()), Decimal(0))
        outflow = sum((Decimal(r['amount_edge']) for r in rows if r['from'].lower() == address.lower()), Decimal(0))
        cp = defaultdict(lambda: {'in_events': 0, 'out_events': 0, 'in_edge': Decimal(0), 'out_edge': Decimal(0)})
        for r in rows:
            inbound = r['to'].lower() == address.lower()
            other = (r['from'] if inbound else r['to']).lower()
            key = 'in' if inbound else 'out'
            cp[other][key + '_events'] += 1
            cp[other][key + '_edge'] += Decimal(r['amount_edge'])
        summary['scopes'][label] = {
            'address': address, 'coverage': coverage,
            'received_edge': str(inflow), 'sent_edge': str(outflow), 'net_edge': str(inflow - outflow),
            'counterparties': {k: {kk: str(vv) for kk, vv in val.items()} for k, val in cp.items()},
        }
        with (dest / f'{label}.csv').open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        allrows.extend(rows)
        print(f'{label}: complete={coverage["complete"]} inbound={inflow} outgoing={outflow}', flush=True)
    # Different scanned addresses may share the same event; unique by tx + log index.
    unique = {(r['tx_hash'], r['log_index']): r for r in allrows}
    with (dest / 'unique_edge_transfers.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(sorted(unique.values(), key=lambda r: (r['timestamp'], r['tx_hash'], r['log_index'])))
    (dest / 'summary.json').write_text(json.dumps(summary, indent=2))
    print('Output:', dest.resolve())

if __name__ == '__main__':
    main()
