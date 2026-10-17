# Fixture PRD: Ambiguous Hardware

## Purpose

This fixture verifies that Refinery separates behavioral requirements from unresolved technical protocol decisions.

## PRD

Build a mobile app that sends content to a connected toy over either BLE or Wi-Fi. The protocol is not final. Parents should pair the toy, send one message, and see whether the toy is online.

## Expected Behavior

- Refinery writes protocol-agnostic FRDs.
- Technical requirements preserve BLE/Wi-Fi as Foundry inputs.
- Gap report flags protocol as a Foundry blocker, not an FRD blocker.
