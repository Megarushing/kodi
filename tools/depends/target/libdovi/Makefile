include ../../Makefile.include LIBDOVI-VERSION ../../download-files.include
DEPS = ../../Makefile.include Makefile LIBDOVI-VERSION ../../download-files.include \
                                       dolby_vision-Cargo.lock

RUST_TARGET=$(HOST)
ifeq ($(TARGET_PLATFORM),webos)
  RUST_TARGET=armv7-unknown-linux-gnueabi
endif
LIBDYLIB=$(PLATFORM)/target/$(RUST_TARGET)/release/$(BYPRODUCT)

CARGO_ENV_VARS = RUSTUP_HOME=$(NATIVEPREFIX)/.rustup \
	               CARGO_HOME=$(NATIVEPREFIX)/.cargo
ifeq ($(TARGET_PLATFORM),webos)
  CARGO_ENV_VARS += CARGO_TARGET_ARMV7_UNKNOWN_LINUX_GNUEABI_LINKER=$(TOOLCHAIN)/bin/$(HOST)-gcc
endif
CARGO = $(CARGO_ENV_VARS) $(NATIVEPREFIX)/bin/cargo

CARGO_BASE_OPTS = --manifest-path $(PLATFORM)/dolby_vision/Cargo.toml
ifeq ($(CROSS_COMPILING),yes)
  CARGO_BASE_OPTS += --target $(RUST_TARGET)
endif

CARGO_BUILD_OPTS = --locked \
	--library-type staticlib \
	--profile release \
	--prefix $(PREFIX) \
	$(CARGO_BASE_OPTS)

all: .installed-$(PLATFORM)

$(PLATFORM): $(DEPS) | $(TARBALLS_LOCATION)/$(ARCHIVE).$(HASH_TYPE)
	rm -rf $(PLATFORM)/*; mkdir -p $(PLATFORM)
	cd $(PLATFORM); $(ARCHIVE_TOOL) $(ARCHIVE_TOOL_FLAGS) $(TARBALLS_LOCATION)/$(ARCHIVE)
	cp dolby_vision-Cargo.lock $(PLATFORM)/dolby_vision/Cargo.lock
	$(CARGO) fetch  --locked $(CARGO_BASE_OPTS)

$(LIBDYLIB): $(PLATFORM)
	$(CARGO) cbuild $(CARGO_BUILD_OPTS)

.installed-$(PLATFORM): $(LIBDYLIB)
	$(CARGO) cinstall $(CARGO_BUILD_OPTS)

	touch $@

clean:
	cd $(PLATFORM); $(CARGO) clean
	rm -f .installed-$(PLATFORM)

distclean::
	rm -rf $(PLATFORM) .installed-$(PLATFORM)
