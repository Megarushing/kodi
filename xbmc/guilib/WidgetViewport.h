/*
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

namespace KODI::GUI
{
constexpr bool ShouldUpdateWidgetRow(int rowIndex, int focusedIndex)
{
  // Empty AF3 rows occupy no layout space. Use configured order so their collapsed
  // geometry cannot make every unloaded row appear to be just below the viewport.
  return rowIndex >= focusedIndex - 2 && rowIndex <= focusedIndex + 3;
}
} // namespace KODI::GUI
