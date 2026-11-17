{
  description = "granger: pull images (and their captions) out of illustrated books";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # Python libraries the extractor imports.
      pythonDeps = ps: [
        ps.pymupdf # PDF parsing/rendering, image decoding
        ps.pillow # image writing
        ps.numpy # figure segmentation on page-sized artwork
        ps.scipy # connected components / morphology for the same
        ps.mobi # unpacks AZW3/MOBI (KindleUnpack) into EPUB
      ];
    in
    {
      packages = forAllSystems (pkgs: rec {
        granger = pkgs.python3Packages.buildPythonApplication {
          pname = "granger";
          version = "0.1.0";
          pyproject = true;
          src = pkgs.lib.fileset.toSource {
            root = ./.;
            fileset = pkgs.lib.fileset.unions [
              ./pyproject.toml
              ./granger
            ];
          };
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = pythonDeps pkgs.python3Packages;
          # Font used to typeset captions under images from reflowable EPUBs.
          makeWrapperArgs = [
            "--set-default"
            "GRANGER_FONT"
            "${pkgs.dejavu_fonts}/share/fonts/truetype/DejaVuSans.ttf"
            "--set-default"
            "GRANGER_FONT_BOLD"
            "${pkgs.dejavu_fonts}/share/fonts/truetype/DejaVuSans-Bold.ttf"
          ];
          pythonImportsCheck = [ "granger" ];
        };
        default = granger;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ (pkgs.python3.withPackages pythonDeps) ];
          GRANGER_FONT = "${pkgs.dejavu_fonts}/share/fonts/truetype/DejaVuSans.ttf";
          GRANGER_FONT_BOLD = "${pkgs.dejavu_fonts}/share/fonts/truetype/DejaVuSans-Bold.ttf";
        };
      });
    };
}
