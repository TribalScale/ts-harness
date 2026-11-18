# Reference feature for the notes page. Name each feature file after the ticket it covers
# (JMT-14-week-view.feature): TS Harness checks a ticket's change includes its feature file.
Feature: Notes page
  As a signed-in user
  I want to see and add notes
  So that the team can share short updates

  Scenario: See the notes
    Given I am signed in as a user
    And the API has notes:
      | title          | body                    |
      | Shift handover | Line 3 back up at 10:00 |
    When I open "/notes"
    Then I see the heading "Notes"
    And I see the heading "Shift handover"
    And I see "Line 3 back up at 10:00"

  Scenario: Add a note
    Given I am signed in as a user
    When I open "/notes"
    And I fill "Title" with "Weekly plan"
    And I click "Add note"
    Then I see the heading "Weekly plan"
    And I do not see "No notes yet."

  Scenario: A duplicate title is refused
    Given I am signed in as a user
    And the API has notes:
      | title       |
      | Weekly plan |
    When I open "/notes"
    And I fill "Title" with "Weekly plan"
    And I click "Add note"
    Then I see "A note with this title already exists"

  Scenario: Signing in is required
    Given I am not signed in
    When I open "/notes"
    Then I see the heading "Sign in to continue"
