VERSION := 0.1
REPOSITORY := localhost
IMAGE := md-indra-ocr

ifneq (,$(wildcard .env))
    include .env
    export
endif

build:
	docker build -t $(REPOSITORY)/messydesk/$(IMAGE):$(VERSION) .

start:
	docker run -d --name $(IMAGE) \
		-p 9013:9013 \
		--replace \
		-v indra-models:/models \
		--restart unless-stopped \
		$(REPOSITORY)/messydesk/$(IMAGE):$(VERSION)

stop:
	docker stop $(IMAGE)
	docker rm $(IMAGE)

restart:
	docker stop $(IMAGE)
	docker rm $(IMAGE)
	$(MAKE) start

bash:
	docker exec -it $(IMAGE) bash
