/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <utility>
#include <vector>

namespace KODI::VIDEO::GUILIB
{
// LOCAL PATCH (megarushing): which season / episode to select when a list opens.
struct NextUnwatchedEntry
{
  int index;
  int season;
  int episode; // 0 for seasons
  bool watched; // fully watched
  bool started; // partly watched (seasons only)
};

/*! \brief The first unwatched entry after the last watched or started one, so a show opens
 where it was left off. Falls back to the first unwatched entry when nothing unwatched comes
 after it. \return the entry's index, -1 when everything is watched.
 */
inline int GetNextUnwatchedIndex(const std::vector<NextUnwatchedEntry>& entries)
{
  bool any{false};
  std::pair<int, int> last{};
  for (const auto& e : entries)
  {
    if ((e.watched || e.started) && (!any || std::pair{e.season, e.episode} > last))
    {
      last = {e.season, e.episode};
      any = true;
    }
  }

  const NextUnwatchedEntry* next{nullptr};
  const NextUnwatchedEntry* first{nullptr};
  for (const auto& e : entries)
  {
    if (e.watched)
      continue;
    const std::pair key{e.season, e.episode};
    if (!first || key < std::pair{first->season, first->episode})
      first = &e;
    if ((!any || key >= last) && (!next || key < std::pair{next->season, next->episode}))
      next = &e;
  }

  if (next)
    return next->index;
  return first ? first->index : -1;
}
} // namespace KODI::VIDEO::GUILIB
