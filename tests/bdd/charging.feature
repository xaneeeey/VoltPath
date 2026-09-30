Feature: Charging decisions for EV fleets
  As a fleet manager I want the platform to tell me where and when to charge
  so that no vehicle is stranded and energy costs stay low.

  Background:
    Given the VoltPath stack is running
    And I am signed in as "fleetmanager@acme.test"

  Scenario: A driving vehicle gets reachable chargers ranked by road distance
    Given a vehicle of my fleet that is reporting live telemetry
    When I ask for reachable chargers
    Then I get at least 1 charger with a free connector
    And the chargers are ordered by road distance
    And every suggested charger is within the predicted range

  Scenario: Overnight charging is planned in the cheapest tariff windows
    Given a vehicle of my fleet that is reporting live telemetry
    When I plan a charge to 80 percent before 07:00
    Then the optimised cost is not higher than charging immediately
    And no energy is scheduled in the evening peak tariff

  Scenario: The copilot proposes but never books on its own
    Given a vehicle of my fleet that is reporting live telemetry
    When I ask the copilot to book the cheapest overnight charge
    Then the copilot returns an action awaiting my confirmation
    And no charging schedule exists for that action until I confirm it
    When I confirm the action
    Then a charging schedule is created by the agent
