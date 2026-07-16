' Launcher for the realbook:// URL protocol.
' If the Song Finder server is already running, just open the browser tab.
' Otherwise start Start_Song_Finder.bat (which opens the browser itself).

Dim sh, http, running
Set sh = CreateObject("WScript.Shell")

running = False
On Error Resume Next
Set http = CreateObject("MSXML2.XMLHTTP")
http.open "GET", "http://127.0.0.1:8765/", False
http.send
If Err.Number = 0 Then running = True
On Error GoTo 0

If running Then
    sh.Run "http://127.0.0.1:8765/"
Else
    sh.Run """C:\Users\stats\langchain\Practice\real_book_song_finder\Start_Song_Finder.bat""", 7, False
End If
