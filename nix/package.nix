# The plugin as a python package. The python dependencies render
# from pyproject.toml through pyproject-nix, so pyproject.toml stays
# the single source of dependency truth.
#
# Usable without flakes: pyproject.nix's root default.nix is a plain
# `{ lib }` function, so a non-flake consumer can import a fetched
# copy and pass it as the pyproject-nix argument. ./default.nix
# composes the full wrapped papis.
{ buildPythonPackage, python, pyproject-nix }:

buildPythonPackage (
  (pyproject-nix.lib.project.loadPyproject {
    projectRoot = ../.;
  }).renderers.buildPythonPackage { inherit python; }
)
