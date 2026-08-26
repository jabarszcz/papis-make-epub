# Compose papis with plugins into one wrapped executable.
#
# papis discovers entry-point plugins in its own python environment,
# and plugin code runs inside the papis process, so a plugin's
# external run-time programs must be reachable from the PATH of the
# papis executable itself (papis-make-epub's optional math renderers
# call texmath, node, and lasem-render), and its environment
# variables must be set there too (the mathjax renderer resolves its
# node library through NODE_PATH). This builds the shared python
# environment and wraps the papis binary with the given programs on
# PATH and the given variables set.
#
# Nothing here is specific to papis-make-epub; a withPlugins-style
# wrapper like this could move upstream, to papis' own flake or to
# the nixpkgs papis package.
#
# ./default.nix composes this wrapper with the plugin and the math
# renderers' programs.
#
# The wrapper takes the interpreter: on an interpreter carrying
# packageOverrides, withPackages sees the overridden set, while its
# pkgs.python refers to the interpreter from before the override.
{ lib, symlinkJoin, makeWrapper }:

{ python # interpreter whose set provides papis and the plugins
, plugins ? ps: [ ] # plugin selector, e.g. ps: [ ps.papis-make-epub ]
, extraPrograms ? [ ] # packages whose programs plugins run at run time
, extraEnv ? { } # variables set on the papis binary, e.g. NODE_PATH
}:

let
  env = python.withPackages (ps: [ ps.papis ] ++ plugins ps);
  wrapArgs = lib.optionals (extraPrograms != [ ])
      [ "--prefix" "PATH" ":" (lib.makeBinPath extraPrograms) ]
    ++ lib.concatLists
      (lib.mapAttrsToList (name: value: [ "--set" name value ]) extraEnv);
in
symlinkJoin {
  name = "papis-with-plugins";
  paths = [ env ];
  meta.mainProgram = "papis";
  nativeBuildInputs = [ makeWrapper ];
  postBuild = lib.optionalString (wrapArgs != [ ]) ''
    wrapProgram $out/bin/papis ${lib.escapeShellArgs wrapArgs}
  '';
}
