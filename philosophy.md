This project builds a toy physics simulation, not a production system. Correctness of
numerical computation matters more than abstraction elegance. Every function operates on
numpy arrays and returns numpy arrays -- no custom wrapper types. Performance is secondary
to correctness until profiling proves a bottleneck exists. The simulation world is pure
environment with no decision-making logic -- entities that act in this world belong in
separate files built in later phases. Keep the physics deterministic and reproducible.
