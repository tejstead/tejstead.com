.PHONY: deploy serve-math

deploy:
	./deploy.sh

# Serve a heilbronn-site build the way math.caddy does:
#   make serve-math DIST=../heilbronn-site/dist
DIST ?= ../heilbronn-site/dist
serve-math:
	docker run --rm \
	  -v "$(abspath $(DIST))":/srv:ro \
	  -v "$(PWD)/caddy/Caddyfile.local":/etc/caddy/Caddyfile:ro \
	  -p 8081:8081 caddy:2-alpine
