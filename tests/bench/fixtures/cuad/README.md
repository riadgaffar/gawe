# Synthetic CUAD-shaped fixtures

Three made-up contracts and a cut-down `master_clauses.csv` in the shape of CUAD v1: a `Filename`
column, then the clause and `-Answer` columns for each category the benchmark reads (spelled as
the real file spells them), and empty PDF placeholders filed by contract type as CUAD files its
PDFs. No CUAD text is included. They let the harness be tested offline; the real data is downloaded by
`make bench-data` into `.bench-data/` and never committed.
