Banking demo service landscape
This folder contains six small, independent Java 17/Maven applications. They model a three-layer banking payment flow:

Channel layer — customer-onboarding-api and payment-initiation-api
Processing layer — transaction-validation-service and ledger-posting-service
Data layer — account-query-service and regulatory-reporting-service
Each project is a separate runnable repository-shaped folder. From a service folder run:

mvn package
java -jar target/<service-name>-1.0.0.jar
Every run writes a log to that service's logs/ directory. The transaction-validation service intentionally demonstrates an IndexOutOfBoundsException caused by positional rule lookup; ledger-posting demonstrates database connection-pool exhaustion caused by a leaked retry connection. Both exit normally after logging, so they are safe repeatable incident inputs. Copy their generated logs, or use the ready-to-submit payloads in ../data/new-incident.json.

The intended happy-path flow is:

payment-initiation-api -> transaction-validation-service -> ledger-posting-service -> account-query-service -> regulatory-reporting-service


transaction-validation-service: IndexOutOfBoundsException from an invalid positional risk-rule lookup.
ledger-posting-service: DatabaseConnectionPoolExhaustedException from leaked/repeated connection acquisition.