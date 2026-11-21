/*
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

namespace KODI::GUI
{
constexpr bool ShouldUpdateWidgetRow(float rowTop,
                                     float rowHeight,
                                     float scroll,
                                     float viewport,
                                     bool focused)
{
  // Prefetch half a viewport on either side so remote navigation need not wait at the edge.
  const float lookahead = viewport * 0.5f;
  return focused || (rowTop + rowHeight > scroll - lookahead &&
                     rowTop < scroll + viewport + lookahead);
}
} // namespace KODI::GUI
