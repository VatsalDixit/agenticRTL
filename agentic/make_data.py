#!/usr/bin/env python3
"""
Write the Parquet files the loop is scored on, into agentic/data/.

    python agentic/make_data.py            write whatever is missing
    python agentic/make_data.py --force    write all of them again

Every file is written by DuckDB with its default Parquet settings and Snappy,
the way a Parquet file in the wild is written: 122,880 rows per row group,
and a page as long as the column chunk makes it (up to 15 MB here).

  taxi.parquet            NYC TLC yellow taxi trips, January 2024: 2,964,624
                          rows, 19 columns, from the TLC's public download
  tpch1_<table>.parquet   the eight TPC-H tables at scale factor 1, generated
                          by DuckDB's tpch extension

Needs the duckdb package (pip install duckdb). The shipped corpus was written
by DuckDB 1.5.5; another version may cut pages differently, which changes
the stimulus, so a different version is reported.
"""

import argparse
import os
import sys
import urllib.request

KIT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(KIT, 'data')
TAXI_URL = ('https://d37ci6vzurychx.cloudfront.net/trip-data/'
            'yellow_tripdata_2024-01.parquet')
TPCH_TABLES = ('lineitem', 'orders', 'customer', 'part', 'partsupp',
               'supplier', 'nation', 'region')
WRITTEN_WITH = '1.5.5'


def sql_path(path):
    return path.replace('\\', '/').replace("'", "''")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--force', action='store_true')
    args = ap.parse_args()
    try:
        import duckdb
    except ImportError:
        print('needs the duckdb package: pip install duckdb')
        return 2
    if duckdb.__version__ != WRITTEN_WITH:
        print('note: DuckDB %s, the shipped corpus was written by %s; pages may '
              'be cut differently' % (duckdb.__version__, WRITTEN_WITH))
    os.makedirs(DATA, exist_ok=True)
    con = duckdb.connect()

    taxi = os.path.join(DATA, 'taxi.parquet')
    if args.force or not os.path.exists(taxi):
        src = os.path.join(DATA, 'src', os.path.basename(TAXI_URL))
        if not os.path.exists(src):
            os.makedirs(os.path.dirname(src), exist_ok=True)
            print('downloading %s' % TAXI_URL)
            urllib.request.urlretrieve(TAXI_URL, src + '.part')
            os.replace(src + '.part', src)
        con.sql("COPY (SELECT * FROM read_parquet('%s')) TO '%s' "
                "(FORMAT parquet, COMPRESSION snappy)" % (sql_path(src), sql_path(taxi)))
        print('wrote %s' % taxi)

    todo = [t for t in TPCH_TABLES
            if args.force or not os.path.exists(os.path.join(DATA, 'tpch1_%s.parquet' % t))]
    if todo:
        con.sql('INSTALL tpch')
        con.sql('LOAD tpch')
        con.sql('CALL dbgen(sf=1)')
        for table in todo:
            path = os.path.join(DATA, 'tpch1_%s.parquet' % table)
            con.sql("COPY %s TO '%s' (FORMAT parquet, COMPRESSION snappy)"
                    % (table, sql_path(path)))
            print('wrote %s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
