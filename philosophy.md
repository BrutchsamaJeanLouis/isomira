This project prioritises correctness over cleverness. Every function does one thing.
Error handling is explicit -- no silent swallowing of exceptions. Dependencies are
minimal: stdlib + requests + pytest. Code should be readable by one person six months
from now without any comments explaining "why" -- the structure itself should make
intent obvious. If a choice is between simplicity and performance, choose simplicity
until profiling proves otherwise.
