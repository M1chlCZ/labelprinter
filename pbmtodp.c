/**
 * pbmtodp.c
 *
 * Conversion from plain PBM/PNM one-bit bitmap format to
 * Intermec Direct Protocol PRBUF RLL/RLE bitmap image format.
 *
 * Copyright (c) 2012, Intermec Technologies Corp.
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without 
 * modification, are permitted provided that the following conditions are met:
 *   1. Redistributions of source code must retain the above copyright notice, 
 *      this list of conditions and the following disclaimer.
 *   2. Redistributions in binary form must reproduce the above copyright 
 *      notice, this list of conditions and the following disclaimer in the 
 *      documentation and/or other materials provided with the distribution.
 *   3. Neither the name of the Intermec Technologies Corp. nor the names of 
 *      its contributors may be used to endorse or promote products derived 
 *      from this software without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" 
 * AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE 
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE 
 * ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE 
 * LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR 
 * CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF 
 * SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS 
 * INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN 
 * CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) 
 * ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE 
 * POSSIBILITY OF SUCH DAMAGE.
 *
 */


/* -------- Includes -------------------------- */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>


/* -------- Defines --------------------------- */
#define HEADBUFSIZE     512
#define MAXDOTWIDTH     0xFFFF
#define MAXDOTHEIGHT    0xFFFF
#define WIDTHBUFMARGIN  32
#define BITS_PER_BYTE   8
#define RLLMAXPIXELRUN  127
#define TRUE            1
#define FALSE           0

#define ERRSTR_MEMORY  "Failed to allocate read buffer (%d bytes)\n"
#define ERRSTR_MEMOUT  "Failed to allocate write buffer (%d bytes)\n"
#define ERRSTR_FORMAT  "File format not recognized (%d). Use plain PBM.\n"
#define ERRSTR_WIDTH   "Image width (%u) too large, maximum (%u) supported.\n"
#define ERRSTR_HEIGHT  "Image height (%u) too large, maximum (%u) supported.\n"
#define ERRSTR_CORRUPT "Image data corrupt (%u).\n"
#define ERRSTR_CONVERT "Image conversion fail (%u).\n"


/* -------- Function Prototypes --------------- */
static int pbm_encode_to_rll(char *outbuf, unsigned int *outbuflen,
                             char *buf, unsigned int width);
static char read_one_pixrow(char *buf, unsigned int bufsize, 
                            unsigned int width, FILE *instream);


/* -------- Global Functions ------------------ */
/**
 * main() description here..
 *
 */
int main(int argc, char *argv[])
{
  unsigned int i = 0;
  unsigned int j = 0;
  unsigned int width = 0;
  unsigned int height = 0;
  unsigned int bufsize = HEADBUFSIZE;
  unsigned int outbuflen = 0;
  unsigned int outbufsize = 0;
  unsigned char rv = 1;
  char *buf = NULL;
  char *outbuf = NULL;

  buf = calloc(bufsize, 1);

  /* Check memory allocation */
  if(!buf)
  {
    fprintf(stderr, ERRSTR_MEMORY, bufsize);
    goto cleanup;
  }

  /* Read header, file format */
  if(!fgets(buf, (bufsize-1), stdin))
  {
    fprintf(stderr, ERRSTR_FORMAT, 1);
    goto cleanup;
  }

  /* Check file format */
  if(strncmp(buf, "P1", 2))
  {
    fprintf(stderr, ERRSTR_FORMAT, 2);
    goto cleanup;
  }

  /* Read header, bitmap dimensions */
  if(!fgets(buf, (bufsize-1), stdin))
  {
    fprintf(stderr, ERRSTR_FORMAT, 3);
    goto cleanup;
  }

  /* Parse bitmap dimensions */
  if(sscanf(buf, "%u %u", &width, &height) != 2)
  {
    fprintf(stderr, ERRSTR_FORMAT, 4);
    goto cleanup;
  }

  /* Check bitmap width */
  if(width > MAXDOTWIDTH)
  {
    fprintf(stderr, ERRSTR_WIDTH, width, MAXDOTWIDTH);
    goto cleanup;
  }

  /* Check bitmap height */
  if(height > MAXDOTHEIGHT)
  {
    fprintf(stderr, ERRSTR_HEIGHT, height, MAXDOTHEIGHT);
    goto cleanup;
  }

  /* Free header buffer buf */
  free(buf);

  /* Allocate data buffer buf */
  bufsize = width + WIDTHBUFMARGIN;
  buf = calloc(bufsize, 1);
  
  /* Check memory allocation */
  if(!buf)
  {
    fprintf(stderr, ERRSTR_MEMORY, bufsize);
    goto cleanup;
  }

  outbufsize = width + WIDTHBUFMARGIN;
  outbuf = calloc(outbufsize, 1);
  
  /* Check memory allocation */
  if(!outbuf)
  {
    fprintf(stderr, ERRSTR_MEMOUT, bufsize);
    goto cleanup;
  }

  /* Output PRBUF RLL header signature */
  fprintf(stdout, "%c%c", 0x40, 0x02);

  /* Output PRBUF RLL header width */
  fprintf(stdout, "%c", ((width >> 8) & 0xFF));
  fprintf(stdout, "%c", (width & 0xFF));

  /* Output PRBUF RLL header height */
  fprintf(stdout, "%c", ((height >> 8) & 0xFF));
  fprintf(stdout, "%c", (height & 0xFF));

  /* Read data line by line */
  for(i = 0; i < height; i++)
  {
    if(read_one_pixrow(buf, (bufsize-1), width, stdin))
    {
      pbm_encode_to_rll(outbuf, &outbuflen, buf, width);

      for(j = 0; j < outbuflen; j++)
      {
        fprintf(stdout, "%c", outbuf[j]);
      }
    }
    else
    {
      fprintf(stderr, ERRSTR_CORRUPT, 1);
      goto cleanup;
    }
  }

  /* Provide OK return value */
  rv = 0;

  /* Cleanup; free memory and exit */
 cleanup:
  if(buf)
  {
    free(buf);
  }
  if(outbuf)
  {
    free(outbuf);
  }

  return rv;
}


/* -------- Local Functions ------------------- */
/**
 * pbm_encode_to_rll() description here..
 *
 */
static int pbm_encode_to_rll(char *outbuf, unsigned int *outbuflen,
                             char *buf, unsigned int width)
{
  unsigned char lastbit = 0;
  unsigned int bufpos = 0;
  unsigned int runcount = 1;

  char endbuf = FALSE;
  char maxrun = FALSE;

  *outbuflen = 0;
  lastbit = buf[0];

  if(buf[bufpos] == '1')
  {
    outbuf[*outbuflen] = 0x00;
    (*outbuflen)++;
  }

  while(!endbuf)
  {
    while(TRUE)
    {
      bufpos++;

      if(bufpos >= width)
      {
        endbuf = TRUE;
        break;
      }
      
      if(runcount >= RLLMAXPIXELRUN)
      {
        if(buf[bufpos] == lastbit)
        {
          maxrun = TRUE;
          break;
        }
      }

      if(buf[bufpos] != lastbit)
      {
        lastbit = buf[bufpos];
        break;
      }
      
      runcount++;
    }

    outbuf[*outbuflen] = runcount;
    (*outbuflen)++;

    if(endbuf)
    {
      /* End of input buffer, dont do anything */
    }
    else if(maxrun)
    {
      /* Max run of same pixel type, insert zero of other type */
      maxrun = FALSE;
      outbuf[*outbuflen] = 0x00;
      (*outbuflen)++;
      runcount = 1;
    }
    else
    {
      /* Pixel type changed, reset counter to one, as we have one new pix */
      runcount = 1;
    }
  }

  if(lastbit == '1')
  {
    outbuf[*outbuflen] = 0x00;
    (*outbuflen)++;
  }

  return 0;
}


/**
 * read_one_pixrow() description here..
 *
 */
static char read_one_pixrow(char *buf, unsigned int bufsize, 
                            unsigned int width, FILE *instream)
{
  unsigned int readbytes = 0;
  int rv = TRUE;

  while(readbytes < width)
  {
    if(fgets(buf, (bufsize-readbytes), instream))
    {
      while((buf[0] == '0') || (buf[0] == '1'))
      {
        buf++;
        readbytes++;
      }
    }
    else
    {
      rv = FALSE;
      break;
    }
  }

  return rv;
}
