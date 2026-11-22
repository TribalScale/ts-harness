# Bookshelf: reading list

A tiny web app where one person keeps a list of books they want to read.

## Goal

Let a reader add books to a reading list and mark them as read. No accounts, no sharing.

## Features

### 1. Add a book

- A form with a title (required, 1-200 characters) and an author (optional, up to 120 characters).
- Submitting adds the book to the top of the list; an empty title shows "Title is required" and adds nothing.
- The list survives a page reload (stored on the server).

### 2. Mark a book as read

- Each book in the list has a "Read" checkbox.
- Checking it moves the book to a "Finished" section below the list, with the date it was finished.
- Unchecking moves it back.

## Out of scope

Search, editing a book, deleting a book, accounts, covers, ratings.

## Acceptance

- Both features have unit tests and one end-to-end test each.
- CI passes on the pull request.
