/*
 * Build-time configuration for this vendored copy of Opus.
 *
 * As it arrived from Omi's tree this file ended with
 *
 *     #include "../../config.h"
 *
 * which reached out of the library and into their application's headers --
 * a file that also pulls in <haly/nrfy_gpio.h> and defines their pin map and
 * codec ids. None of that has anything to do with Opus; the only symbol the
 * codec actually needs from it is CONFIG_OPUS_MODE. So the reach is cut and
 * the symbol defined here, which is what lets this directory be dropped into
 * another project without dragging an unrelated header behind it.
 *
 * Everything else the original file documented -- bitrate, complexity, VBR --
 * is not consumed by any source in this tree. Those are set at runtime
 * through the encoder API instead, which is where this project sets them.
 */

#define CONFIG_OPUS_MODE_CELT   (1 << 0)
#define CONFIG_OPUS_MODE_SILK   (1 << 1)
#define CONFIG_OPUS_MODE_HYBRID (CONFIG_OPUS_MODE_CELT | CONFIG_OPUS_MODE_SILK)

/*
 * CELT only.
 *
 * SILK is the speech-tuned half of Opus and would be the obvious choice for a
 * recorder, but it costs considerably more CPU and RAM, and the mode is fixed
 * at build time because the two halves are compiled in separately. CELT with
 * OPUS_APPLICATION_RESTRICTED_LOWDELAY is what Omi ships and has run on this
 * same part at 32 kbps; that is a measurement this project has not had to
 * make for itself. Hybrid is not supported by this copy.
 */
#define CONFIG_OPUS_MODE CONFIG_OPUS_MODE_CELT
