"""Intake sources: files and Drive links a requester shares when starting a program.

Inbound messages become small pointers (``sources``), uploaded bytes live in the
``intake`` object store (``blobs``), and a workflow barrier turns pointers into
capped text records (``resolve``, ``gather``).
"""
