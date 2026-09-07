PLUGIN := dev.gsr.sdPlugin
PLUGINS_DIR := $(HOME)/.config/opendeck/plugins

.PHONY: test validate check package install icons clean

test:
	python3 -m unittest discover -s tests -t . -v

validate:
	python3 scripts/validate_plugin.py
	python3 -m compileall -q $(PLUGIN) scripts tests
	sh -n $(PLUGIN)/run.sh

check: validate test

package:
	@python3 scripts/package.py

# Regenerates the action icons from the glyphs the keys are drawn with. The
# output is committed, so this is run after changing a glyph, not on build.
icons:
	python3 scripts/make_icons.py

# Copied rather than symlinked: OpenDeck resolves a plugin's property
# inspectors relative to the real directory, and refuses paths that
# canonicalise outside it.
install:
	rm -rf $(PLUGINS_DIR)/$(PLUGIN)
	mkdir -p $(PLUGINS_DIR)
	cp -r $(PLUGIN) $(PLUGINS_DIR)/$(PLUGIN)
	find $(PLUGINS_DIR)/$(PLUGIN) -name __pycache__ -type d -exec rm -rf {} +
	rm -f $(PLUGINS_DIR)/$(PLUGIN)/plugin.log
	@echo "installed; restart OpenDeck"

clean:
	rm -rf dist
	find . -name __pycache__ -type d -exec rm -rf {} +
