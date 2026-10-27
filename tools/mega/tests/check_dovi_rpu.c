/* Manual native libdovi smoke test: input is one RPU NAL, optionally Annex B.
 * Cross-build with the webOS SDK and the same libdovi.a used by Kodi. This
 * checks real RPU conversion, not TV image quality or Kodi playback.
 * Golden fixtures are mode-specific: direct FEL mode 2 removes mapping and
 * differs from a mapping-preserved/FEL->MEL->8 golden, despite both being P8.
 */
#include <libdovi/rpu_parser.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char** argv)
{
  if (argc < 2 || argc > 3)
  {
    fprintf(stderr, "usage: check_dovi_rpu <one-profile7-rpu-nal> [output-nal]\n");
    return 2;
  }
  FILE* file = fopen(argv[1], "rb");
  if (!file)
    return 3;
  if (fseek(file, 0, SEEK_END) != 0)
  {
    fclose(file);
    return 4;
  }
  long length = ftell(file);
  if (length <= 0 || length > 1024 * 1024 || fseek(file, 0, SEEK_SET) != 0)
  {
    fclose(file);
    return 5;
  }
  uint8_t* bytes = malloc((size_t)length);
  if (!bytes)
  {
    fclose(file);
    return 6;
  }
  size_t count = fread(bytes, 1, (size_t)length, file);
  fclose(file);
  if (count != (size_t)length)
  {
    free(bytes);
    return 7;
  }
  size_t offset = 0;
  if (count >= 4 && bytes[0] == 0 && bytes[1] == 0 && bytes[2] == 0 && bytes[3] == 1)
    offset = 4;
  else if (count >= 3 && bytes[0] == 0 && bytes[1] == 0 && bytes[2] == 1)
    offset = 3;

  DoviRpuOpaque* input = dovi_parse_unspec62_nalu(bytes + offset, count - offset);
  if (!input)
  {
    free(bytes);
    return 8;
  }
  const DoviRpuDataHeader* before = dovi_rpu_get_header(input);
  int before_profile = before ? before->guessed_profile : -1;
  dovi_rpu_free_header(before);
  int rc = 8;
  if (before_profile != 7 || dovi_convert_rpu_with_mode(input, 2) < 0)
    goto done;
  const DoviData* output = dovi_write_unspec62_nalu(input);
  if (!output)
    goto done;
  DoviRpuOpaque* converted = dovi_parse_unspec62_nalu(output->data, output->len);
  if (!converted)
  {
    dovi_data_free(output);
    goto done;
  }
  const DoviRpuDataHeader* after = dovi_rpu_get_header(converted);
  int after_profile = after ? after->guessed_profile : -1;
  printf("{\"input_profile\":%d,\"output_profile\":%d,\"input_bytes\":%zu,\"output_bytes\":%zu}\n",
         before_profile, after_profile, count - offset, output->len);
  rc = after_profile == 8 ? 0 : 9;
  if (rc == 0 && argc == 3)
  {
    FILE* out = fopen(argv[2], "wb");
    if (!out)
      rc = 10;
    else
    {
      if (fwrite(output->data, 1, output->len, out) != output->len)
        rc = 11;
      if (fclose(out) != 0)
        rc = 12;
    }
  }
  dovi_rpu_free_header(after);
  dovi_rpu_free(converted);
  dovi_data_free(output);
done:
  dovi_rpu_free(input);
  free(bytes);
  return rc;
}
