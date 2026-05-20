# Reset
rm -rf ./dist
rm -f kanjigrid_external_sources_0.0.0.zip
mkdir ./dist

# Create release zip
cp -r src/*.py src/config.json data LICENSE README.md manifest.json ./dist
(cd dist && zip -r ../kanjigrid_external_sources_0.0.0.zip *)
