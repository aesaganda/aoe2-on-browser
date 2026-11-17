#!/bin/sh
# C compiler wrapper for pcrecomp's test drivers: glibc keeps libm separate,
# and the drivers' link lines do not ask for it (macOS doesn't need it).
exec "${CC:-gcc}" "$@" -lm
