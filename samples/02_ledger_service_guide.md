# Ledger Service Guide

The Ledger Service is a fictional internal system used here as a sample document
for testing KAI's ingestion, retrieval, and grounded answering. All content below
is original and written for this repository.

## Overview

The Ledger Service records immutable double-entry transactions for internal teams.
Every posting has a debit account, a credit account, an amount in minor units, and
a currency code. Once written, a posting is never updated or deleted; corrections
are made by writing a compensating posting that references the original.

## Accounts

An account has a stable identifier, a human-readable name, and a type: `asset`,
`liability`, `revenue`, or `expense`. Account balances are derived by summing the
postings that touch the account; they are never stored directly. Closed accounts
reject new postings but remain readable for historical reporting.

## Posting a transaction

Send a `POST /v1/postings` request with a list of legs. The sum of all debit legs
must equal the sum of all credit legs, or the request is rejected with a
`422 unbalanced` error. Each request must include an idempotency key; replaying the
same key returns the original result instead of creating a duplicate.

## Idempotency

Idempotency keys are retained for 72 hours. A retry within that window is safe and
returns the first response. After the window expires, the same key is treated as a
new request, so clients should not reuse keys beyond three days.

## Reconciliation

A nightly reconciliation job verifies that the sum of all debits equals the sum of
all credits across the entire ledger. If the totals disagree, the job halts new
postings and raises a `ledger.imbalance` alert for an operator to investigate.

## Rate limits

The write API allows 50 postings per second per team. Bursts above the limit
receive a `429` response with a `Retry-After` header. Read endpoints are not rate
limited but are served from a replica that may lag the primary by a few seconds.
