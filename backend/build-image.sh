#!/bin/bash

# SupportMesh image build script
# Offers several build options for different scenarios

set -e

# Colours
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Configuration
IMAGE_NAME="supportmesh"
REGISTRY=""  # Set to registry.example.com/ to push to a private registry
VERSION=${VERSION:-latest}
BUILD_ARGS=""

print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_step() {
    echo -e "${BLUE}[STEP]${NC} $1"
}

# Usage help
show_help() {
    cat << EOF
SupportMesh Docker image build tool

Usage: ./build-image.sh [command] [options]

Commands:
    build           build the default image
    build-prod      build the production image
    build-dev       build the development image
    build-test      build the test image
    push            push the image to a registry
    tag             add a tag to the image
    clean           clear the build cache
    help            show this help

Options:
    --no-cache      build without the cache
    --platform      target platform (linux/amd64, linux/arm64)
    --registry      image registry
    --version       version number

Examples:
    ./build-image.sh build-prod
    ./build-image.sh build --no-cache
    ./build-image.sh build --platform linux/amd64,linux/arm64
    ./build-image.sh push --registry my-registry.com
    ./build-image.sh tag --version v1.0.0

EOF
}

# Build the image
build_image() {
    local target=$1
    local no_cache=$2
    local platforms=$3

    print_step "Building image: ${IMAGE_NAME}:${VERSION}"

    # Build arguments
    build_cmd="docker build"

    # Target stage
    if [ -n "$target" ]; then
        build_cmd="$build_cmd --target $target"
    fi

    # Cache options
    if [ "$no_cache" = "true" ]; then
        build_cmd="$build_cmd --no-cache"
        print_warn "Build cache disabled"
    fi

    # Multi-platform support
    if [ -n "$platforms" ]; then
        build_cmd="$build_cmd --platform $platforms"
        print_info "Build platforms: $platforms"
    fi

    # Build arguments
    if [ -n "$BUILD_ARGS" ]; then
        build_cmd="$build_cmd $BUILD_ARGS"
    fi

    # Run the build
    full_tag="${REGISTRY}${IMAGE_NAME}:${VERSION}"
    build_cmd="$build_cmd -t $full_tag -t ${REGISTRY}${IMAGE_NAME}:latest ."

    print_info "Running: $build_cmd"
    eval $build_cmd

    if [ $? -eq 0 ]; then
        print_info "✓ Image built: $full_tag"
    else
        print_error "✗ Image build failed"
        exit 1
    fi
}

# Push the image
push_image() {
    local registry=$1

    if [ -n "$registry" ]; then
        REGISTRY="$registry/"
    fi

    print_step "Pushing image to the registry"

    # Push the version tag
    docker push ${REGISTRY}${IMAGE_NAME}:${VERSION}

    # Push the latest tag
    docker push ${REGISTRY}${IMAGE_NAME}:latest

    print_info "✓ Image pushed"
}

# Tag the image
tag_image() {
    local new_version=$1

    print_step "Tagging image: $new_version"

    docker tag ${REGISTRY}${IMAGE_NAME}:${VERSION} ${REGISTRY}${IMAGE_NAME}:${new_version}

    print_info "✓ Tag added: ${REGISTRY}${IMAGE_NAME}:${new_version}"
}

# Clear the build cache
clean_build_cache() {
    print_step "Clearing the Docker build cache"

    docker builder prune -f

    print_info "✓ Build cache cleared"
}

# Parse arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --no-cache)
                NO_CACHE=true
                shift
                ;;
            --platform)
                PLATFORMS="$2"
                shift 2
                ;;
            --registry)
                REGISTRY="$2"
                shift 2
                ;;
            --version)
                VERSION="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done
}

# Main
main() {
    local command=${1:-help}
    shift || true

    # Parse options
    parse_args "$@"

    case $command in
        build)
            build_image "" "$NO_CACHE" "$PLATFORMS"
            ;;
        build-prod)
            build_image "production" "$NO_CACHE" "$PLATFORMS"
            ;;
        build-dev)
            build_image "development" "$NO_CACHE" "$PLATFORMS"
            ;;
        build-test)
            build_image "test" "$NO_CACHE" "$PLATFORMS"
            ;;
        push)
            shift
            push_image "$1"
            ;;
        tag)
            shift
            tag_image "$1"
            ;;
        clean)
            clean_build_cache
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            print_error "Unknown command: $command"
            show_help
            exit 1
            ;;
    esac
}

# Run main
main "$@"