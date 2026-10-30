# The math renderers' external programs and environment variables,
# for whatever runs the plugin: the composed papis (./default.nix)
# and the flake's own scripts, dev shell and test cells.
{ pkgs }:
{
  # texmath converts between TeX and MathML; the mathjax renderer
  # runs node; the lasem renderer runs lasem-render
  programs = [ (pkgs.callPackage ./texmath.nix { }) pkgs.nodejs pkgs.lasem ];
  # the mathjax renderer resolves its node library through NODE_PATH;
  # mathjax-node-cli bundles it
  env.NODE_PATH =
    "${pkgs.mathjax-node-cli}/lib/node_modules/mathjax-node-cli/node_modules";
}
