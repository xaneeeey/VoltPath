Feature: Privacy and tenant isolation
  Scenario: A viewer sees vehicle locations only at neighbourhood precision
    Given the VoltPath stack is running
    And I am signed in as "viewer@acme.test"
    And a vehicle of my fleet that is reporting live telemetry
    When I open the vehicle
    Then its location is masked to about 5 km

  Scenario: One tenant cannot see another tenant's vehicles
    Given the VoltPath stack is running
    And I am signed in as "fleetmanager@acme.test"
    And a vehicle of my fleet that is reporting live telemetry
    When "fleetmanager@zenith.test" requests that vehicle
    Then the response is "404"

  Scenario: Every copilot tool call is audited
    Given the VoltPath stack is running
    And I am signed in as "viewer@acme.test"
    When I ask the copilot "How is the fleet doing?"
    And I am signed in as "admin@acme.test"
    Then the audit trail contains an agent entry for "tool:fleet_summary"
