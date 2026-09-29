# Makefile — BJGLLC/.github's one check entrypoint (Review v4 spec §5, P3-A). ci runs `make check`
# through this repo's own ./actions/ci-check; humans and the janitor run the same target.
.PHONY: check
check:
	cd scripts && python3 -m unittest -v
