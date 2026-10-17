/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "utils/MemUtils.h"

#include <stdlib.h>

#include <androidjni/ActivityManager.h>
#include <androidjni/Context.h>

namespace KODI
{
namespace MEMORY
{

void* AlignedMalloc(size_t s, size_t alignTo)
{
  // LOCAL PATCH (megarushing): xbmc/xbmc#29091 - on failure posix_memalign leaves p
  // untouched, so the old code returned stack garbage that callers took as valid.
  void* p = nullptr;
  if (posix_memalign(&p, alignTo, s) != 0)
    return nullptr;

  return p;
}

void AlignedFree(void* p)
{
  free(p);
}

void GetMemoryStatus(MemoryStatus* buffer)
{
  if (!buffer)
    return;

  auto activityManager = std::make_unique<CJNIActivityManager>(
      CJNIContext::getSystemService(CJNIContext::ACTIVITY_SERVICE));

  if (activityManager)
  {
    CJNIActivityManager::MemoryInfo info;
    activityManager->getMemoryInfo(info);
    if (xbmc_jnienv()->ExceptionCheck())
    {
      xbmc_jnienv()->ExceptionClear();
      return;
    }

    *buffer = {};
    buffer->totalPhys = static_cast<unsigned long>(info.totalMem());
    buffer->availPhys = static_cast<unsigned long>(info.availMem());
  }
}

} // namespace MEMORY
} // namespace KODI
