# Nightly: every design frame TS Harness lists for this app (route and size), captured against
# the real API and sent back for comparison with the design. Does nothing when the harness lists
# no frames. A capture never fails the smoke run.
@smoke
Feature: Smoke: design frames against the real API
  As the team
  I want each delivered screen captured at its design's size every night
  So that drift from the design shows up even when every ticket passed on its own

  Scenario: Capture every design frame
    Given I am signed in as an admin
    Then every design frame is captured
