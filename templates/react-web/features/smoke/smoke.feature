# Nightly smoke run: the built app against the real API and a real database (see
# .github/workflows/nightly-smoke.yml). Only features tagged @smoke run there, and they never run
# in the normal acceptance suite. Keep them few and broad: a failure here becomes a proposed bug.
@smoke
Feature: Smoke: the app works against the real API
  As the team
  I want every page checked against the real API each night
  So that the app and the API cannot drift apart unnoticed

  Scenario: Every page opens after signing in
    Given I am signed in as an admin
    Then every page opens without errors

  Scenario: Add a note
    Given I am signed in as a user
    When I open "/notes"
    And I fill "Title" with a unique "Smoke note"
    And I click "Add note"
    Then I see the heading with the unique "Smoke note"
    And no API call failed
