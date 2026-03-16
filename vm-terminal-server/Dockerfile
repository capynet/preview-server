FROM golang:1.22-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o vm-terminal-server .

FROM alpine:3.19
RUN apk add --no-cache docker-cli
COPY --from=builder /app/vm-terminal-server /usr/local/bin/
EXPOSE 8022
ENTRYPOINT ["vm-terminal-server"]
