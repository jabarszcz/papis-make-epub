{
  description = "papis-make-epub: build EPUB renditions of papis documents";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    # frozen snapshot supplying a curated python 3.11 package set for matrix
    nixpkgs-2505.url = "github:NixOS/nixpkgs/release-25.05";
    # papis-main is the upstream canary for tests pre-trickle-down to nixpkgs
    papis-main.url = "github:papis/papis";
    papis-main.inputs.nixpkgs.follows = "nixpkgs";
    flake-utils.url = "github:numtide/flake-utils";
    pyproject-nix.url = "github:pyproject-nix/pyproject.nix";
    pyproject-nix.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    { self, nixpkgs, nixpkgs-2505, papis-main, flake-utils, pyproject-nix }:
    let
      # The package as an extension of an arbitrary python package
      # set: dependencies (papis included) resolve from the consumer's
      # set, so a consumer with their own papis composes without our
      # involvement. pythonPackagesExtensions applies to every
      # interpreter's set, which is also what the version matrix wants.
      overlay = final: prev: {
        pythonPackagesExtensions = prev.pythonPackagesExtensions ++ [
          (pyfinal: pyprev: {
            papis-make-epub = pyfinal.callPackage ./nix/package.nix {
              inherit pyproject-nix;
            };
          })
        ];
      };

    in
    {
      overlays.default = overlay;
    } // flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          overlays = [ overlay ];
        };
        py = pkgs.python3Packages;

        testPyDeps = ps: [
          ps.papis
          ps.papis-make-epub
          ps.pytest
          ps.pytest-cov
          ps.pytest-regressions
        ];
        # external programs of the math renderers (the mathjax
        # renderer runs node)
        mathPrograms = [
          (pkgs.callPackage ./nix/texmath.nix { })
          pkgs.nodejs
          pkgs.lasem
        ];
        # the mathjax renderer resolves its node library through
        # NODE_PATH; mathjax-node-cli bundles it
        mathjaxNodePath =
          "${pkgs.mathjax-node-cli}/lib/node_modules/mathjax-node-cli/node_modules";
        testPrograms = mathPrograms ++ [ pkgs.epubcheck ];

        # contributor environment: the package's dependency closure
        # plus the type-checking and test halves of the dev tooling
        # (ruff is a standalone binary)
        devEnv = pkgs.python3.withPackages
          (ps: testPyDeps ps ++ [ ps.mypy ]);

        # Run suite for an interpreter (building it), report coverage
        testFor = name: python: pkgs.runCommand "pytest-${name}" {
          nativeBuildInputs =
            [ (python.withPackages testPyDeps) ] ++ testPrograms;
          NODE_PATH = mathjaxNodePath;
          # every external tool is supplied here, so a skipped test
          # is a malfunction (see tests/conftest.py)
          PAPIS_MAKE_EPUB_NO_SKIPS = "1";
        } ''
          # a writable copy: pytest and coverage write cache and
          # data files, and the source tree is read-only in the store
          cp -r ${self} src
          chmod -R +w src
          cd src
          export HOME=$TMPDIR
          pytest --cov --cov-report=term
          touch $out
        '';

        # Version matrix over the declared support range (pyproject
        # requires-python >=3.11): one test-suite cell per
        # interpreter. The floor is 3.11 although papis declares
        # >=3.10: even the 25.05 snapshot cannot evaluate a python
        # 3.10 closure without a growing pile of doc/test-phase
        # skips (its sphinx 8.2 already dropped 3.10).
        matrixCells = {
          # the frozen snapshot: current nixpkgs dropped python 3.10
          # and no longer sustains its 3.11 package set
          python311 = (import nixpkgs-2505 {
            inherit system;
            overlays = [ overlay ];
          }).python311;
          python312 = pkgs.python312;
          python313 = pkgs.python313;
          python314 = pkgs.python314;
        };
        matrixChecks = pkgs.lib.mapAttrs' (name: python:
          pkgs.lib.nameValuePair "test-${name}" (testFor name python))
          matrixCells;

        # the package's console scripts with the math renderers'
        # programs on their PATH. packages.default stays lean: this
        # closure is built on `nix run` only.
        wrappedScripts = pkgs.symlinkJoin {
          name = "papis-make-epub-wrapped";
          paths = [ py.papis-make-epub ];
          nativeBuildInputs = [ pkgs.makeWrapper ];
          postBuild = ''
            for prog in "$out"/bin/*; do
              wrapProgram "$prog" \
                --prefix PATH : ${pkgs.lib.makeBinPath mathPrograms} \
                --set NODE_PATH ${mathjaxNodePath}
            done
          '';
        };

        papisWithPlugin = import ./nix { inherit pkgs pyproject-nix; };

      in {
        packages.default = py.papis-make-epub;

        # the standalone script; papis discovers it as the external
        # command `papis make-epub` (works alongside pkgs.papis)
        apps.default = {
          type = "app";
          program = "${wrappedScripts}/bin/papis-make-epub";
          meta.description = "build EPUB renditions of papis documents";
        };

        # re-capture the golden fixtures; run from the repository
        # root, which supplies the script and receives the captures
        apps.refresh-fixtures = {
          type = "app";
          program = "${pkgs.writeShellApplication {
            name = "refresh-fixtures";
            runtimeInputs = [
              (pkgs.python3.withPackages (ps: [ ps.papis-make-epub ]))
              pkgs.single-file-cli
              pkgs.chromium
            ];
            text = ''
              exec python3 tests/fixtures/refresh.py "$@"
            '';
          }}/bin/refresh-fixtures";
          meta.description = "re-capture the golden fixture pages";
        };

        apps.math-rendering-demo = {
          type = "app";
          program = "${wrappedScripts}/bin/papis-make-epub-demo";
          meta.description =
            "build a renderer-comparison EPUB for device testing";
        };

        # papis with the plugin loaded as an entry point and the math
        # renderers' external programs on the papis binary's PATH,
        # for installation instead of pkgs.papis.
        packages.papis-with-make-epub = papisWithPlugin;

        devShells.default = pkgs.mkShell {
          packages = [ devEnv pkgs.ruff pkgs.git ] ++ testPrograms
            # fixture refresh: single-file renders publisher pages
            # into the self-contained captures the saved-page
            # sources read, driving chromium
            ++ [ pkgs.single-file-cli pkgs.chromium ];
          NODE_PATH = mathjaxNodePath;
          # the checkout is what pytest, the fixture scripts and
          # `papis make-epub` import; the store package in devEnv
          # keeps supplying the scripts and the plugin registration
          shellHook = ''
            export PYTHONPATH="$(git rev-parse --show-toplevel)"
          '';
        };

        checks = matrixChecks // {
          # the canary: papis replaced by papis flake's own build of
          # the development branch. The replacement rides the same
          # pythonPackagesExtensions mechanism as the overlay, so
          # one fixpoint resolves both the plugin and the replaced
          # papis (with an interpreter-level packageOverrides, the
          # extension-defined plugin resolved the original papis
          # through the spliced set and the env carried two papis).
          test-papis-main = testFor "papis-main"
            (pkgs.appendOverlays [
              (final: prev: {
                pythonPackagesExtensions =
                  prev.pythonPackagesExtensions ++ [
                    (pyfinal: pyprev: {
                      papis = papis-main.packages.${system}.papis;
                    })
                  ];
              })
            ]).python3;

          # the launch paths, one --help through each
          entry-smoke = pkgs.runCommand "entry-smoke" { } ''
            export HOME=$TMPDIR
            expect_help() { "$@" --help | grep -q "Build EPUB renditions"; }
            # the entry-point plugin inside the composed papis
            expect_help ${papisWithPlugin}/bin/papis make-epub
            # plain papis dispatching the standalone script from PATH
            (PATH=${pkgs.papis}/bin:${wrappedScripts}/bin:$PATH
             expect_help papis make-epub)
            # the wrapped scripts themselves (what nix run executes)
            expect_help ${wrappedScripts}/bin/papis-make-epub
            ${wrappedScripts}/bin/papis-make-epub-demo --help > /dev/null
            touch $out
          '';

          lint = pkgs.runCommand "ruff-check" {
            nativeBuildInputs = [ pkgs.ruff ];
          } ''
            cd ${self}
            ruff check --no-cache .
            touch $out
          '';

          typecheck = pkgs.runCommand "mypy-check" {
            nativeBuildInputs = [ devEnv ];
          } ''
            cd ${self}
            # /dev/null disables the incremental cache, which mypy
            # would otherwise write into the read-only source dir
            mypy --cache-dir=/dev/null
            touch $out
          '';

        };
      });
}
