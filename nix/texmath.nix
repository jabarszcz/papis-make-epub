# The texmath executable (pandoc's math conversion engine). The
# mathtext renderer runs it for MathML-to-TeX conversion on sources
# whose math carries no TeX annotations (ACM saved pages).
#
# Every package set carries texmath as a haskell library only: the
# cabal "executable" flag defaults to off (upstream describes the
# binary as a test executable), so no nixpkgs attribute provides
# bin/texmath. A by-name nixpkgs package applying these same two
# steps would supersede this file.
{ haskell, haskellPackages }:

haskell.lib.compose.justStaticExecutables
  (haskell.lib.compose.enableCabalFlag "executable"
    haskellPackages.texmath)
