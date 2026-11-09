"""The M12 benchmark harness: Reader accuracy on CUAD and the prompt-injection suite.

It drives the product exactly as shipped (Reader, Planner, validator, policy engine, executor,
mock connectors); only the model provider is wrapped, to meter spend. `gawe` never imports this
package, so nothing here (such as the benchmark-only Reader profile) can reach an app.

Every run that calls a real model needs an explicit spend cap. See docs/m12-design.md.
"""
