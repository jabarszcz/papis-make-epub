# papis wrapped with the plugin and the math renderers' external
# programs; the flake's packages.papis-with-make-epub is this same
# expression. The non-flake copy-paste recipe is in the README.
#
# The fetchTarball default suits non-flake use; the flake passes its
# locked pyproject-nix instead.
{ pkgs ? import <nixpkgs> { }
, pyproject-nix ? import (fetchTarball
    "https://github.com/pyproject-nix/pyproject.nix/archive/master.tar.gz")
    { inherit (pkgs) lib; }
}:

let
  python = pkgs.python3.override {
    packageOverrides = pyfinal: pyprev: {
      papis-make-epub = pyfinal.callPackage ./package.nix {
        inherit pyproject-nix;
      };
    };
  };
  math = import ./math-programs.nix { inherit pkgs; };
in
(pkgs.callPackage ./wrap-papis.nix { }) {
  inherit python;
  plugins = ps: [ ps.papis-make-epub ];
  extraPrograms = math.programs;
  extraEnv = math.env;
}
