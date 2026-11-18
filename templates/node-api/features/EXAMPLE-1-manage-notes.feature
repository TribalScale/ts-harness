# Reference feature for the notes module. Name each feature file after the ticket it covers
# (PROJ-12-create-order.feature): TS Harness checks a ticket's change includes its feature file.
Feature: Manage notes
  As a signed-in user
  I want to keep notes
  So that the team can share short updates

  Scenario: Create a note
    Given I am signed in as a user
    When I send a POST request to "/notes" with:
      """
      { "title": "Shift handover", "body": "Line 3 back up at 10:00" }
      """
    Then the response status should be 201
    And the response field "title" should be "Shift handover"
    And the response field "createdBy" should be "user-1"

  Scenario: A note needs a title
    Given I am signed in as a user
    When I send a POST request to "/notes" with:
      """
      { "title": "" }
      """
    Then the response status should be 400
    And the error code should be "validation_failed"

  Scenario: Titles are unique
    Given I am signed in as a user
    And I send a POST request to "/notes" with:
      """
      { "title": "Weekly plan" }
      """
    When I send a POST request to "/notes" with:
      """
      { "title": "Weekly plan" }
      """
    Then the response status should be 409
    And the error code should be "conflict"

  Scenario: List and read notes
    Given I am signed in as a user
    And I send a POST request to "/notes" with:
      """
      { "title": "Alpha" }
      """
    And I remember the response field "id" as note
    When I send a GET request to "/notes"
    Then the response status should be 200
    And the response field "total" should be 1
    When I send a GET request to "/notes/{note}"
    Then the response field "title" should be "Alpha"

  Scenario: Only admins delete notes
    Given I am signed in as a user
    And I send a POST request to "/notes" with:
      """
      { "title": "Keep me" }
      """
    And I remember the response field "id" as note
    When I send a DELETE request to "/notes/{note}"
    Then the response status should be 403
    Given I am signed in as an admin
    When I send a DELETE request to "/notes/{note}"
    Then the response status should be 204

  Scenario: Signing in is required
    Given I am not signed in
    When I send a GET request to "/notes"
    Then the response status should be 401
    And the error code should be "unauthorized"
