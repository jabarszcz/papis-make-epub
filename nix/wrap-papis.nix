# Compose papis with plugins into one wrapped executable.
#
# papis discovers entry-point plugins in its own python environment,
# and plugin code runs inside the papis process, so a plugin's
# external run-time programs must be reachable from the PATH of the
# papis executable itself (papis-make-epub's optional math renderers
# call texmath, node, and lasem-render), and its environment
# variables must be set there too (the mathjax renderer resolves its
# node library through NODE_PATH). This builds the shared python
# environment and wraps papis and the plugins' console scripts with
# the given programs on PATH and the given variables set, uniformly:
# one list for the whole composition, for simplicity.
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
, extraEnv ? { } # variables set on the wrapped executables, e.g. NODE_PATH
}:

let
  env = python.withPackages (ps: [ ps.papis ] ++ plugins ps);
  pluginPackages = plugins python.pkgs;
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
  # The plugins' console scripts take the same wrapping as papis: run
  # on their own, or dispatched by a papis that found them on PATH,
  # they need the same programs and variables.
  #
  # The wrapping runs with no arguments too, as a workaround for papis
  # reading external commands as UTF-8 text: on the console-script
  # route into papis (pyproject.toml, [project.scripts]) the script is
  # opened for its help line on `papis --help` and on shell
  # completion, so it has to be text, and withPackages emits compiled
  # wrappers; wrapProgramShell leaves a shell script in front. Once
  # papis supports binary external commands, the wrapping can again
  # be conditional on wrapArgs.
  postBuild = ''
    wrap() {
      wrapProgramShell "$out/bin/$(basename "$1")" ${lib.escapeShellArgs wrapArgs}
    }
    wrap papis
    for plugin in ${lib.escapeShellArgs pluginPackages}; do
      for prog in "$plugin"/bin/*; do
        [ -e "$prog" ] && wrap "$prog"
      done
    done
  '';
}
