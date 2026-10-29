/*
 *  Copyright (C) 2026 Team Kodi
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */
#pragma once

#include <chrono>

// LOCAL PATCH (megarushing): VFS HTTP reads must not time out before curl's
// configured low-speed patience. Other inputs retain the existing 20 seconds.
constexpr std::chrono::seconds DemuxReadDeadline(bool vfsHttp, int curlLowSpeedSeconds)
{
  return std::chrono::seconds{vfsHttp && curlLowSpeedSeconds > 20 ? curlLowSpeedSeconds : 20};
}
